import { type Plugin, tool } from "@opencode-ai/plugin"

const AXIS_BASE_URL = "http://127.0.0.1:19100"

class AxisRequestError extends Error {
  constructor(readonly status: number, responseBody: string) {
    super(`AXIS 拒绝自动任务操作（HTTP ${status}）：${responseBody}`)
    this.name = "AxisRequestError"
  }
}

async function requestAxis(path: string, payload: unknown): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(`${AXIS_BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  } catch (error) {
    throw new Error(`无法连接 AXIS ${AXIS_BASE_URL}: ${String(error)}`, { cause: error })
  }
  const text = await response.text()
  if (!response.ok) {
    throw new AxisRequestError(response.status, text)
  }
  try {
    return JSON.parse(text)
  } catch (error) {
    throw new Error(`AXIS 返回了无效 JSON：${text}`, { cause: error })
  }
}

type ActiveLease = {
  taskID: string
  executionToken: string
  timer: ReturnType<typeof setInterval>
}

export const AxisAutomaticTasksPlugin: Plugin = async (_input, options) => {
  const activeLeases = new Map<string, ActiveLease>()
  const configuredInterval = Number(options?.heartbeatIntervalMs)
  const heartbeatIntervalMs = Number.isFinite(configuredInterval) && configuredInterval >= 10
    ? configuredInterval
    : 60_000

  function stopLease(sessionID: string, executionToken?: string): void {
    const lease = activeLeases.get(sessionID)
    if (!lease || (executionToken && lease.executionToken !== executionToken)) return
    clearInterval(lease.timer)
    activeLeases.delete(sessionID)
  }

  function startLease(sessionID: string, taskID: string, executionToken: string): void {
    const existing = activeLeases.get(sessionID)
    if (existing?.taskID === taskID && existing.executionToken === executionToken) return
    stopLease(sessionID)
    const renew = async () => {
      try {
        await requestAxis(
          `/api/v1/automatic-tasks/${encodeURIComponent(taskID)}/heartbeat`,
          { session_id: sessionID, execution_token: executionToken },
        )
      } catch (error) {
        if (error instanceof AxisRequestError && (error.status === 404 || error.status === 409)) {
          stopLease(sessionID, executionToken)
          console.error(`AXIS 自动任务 ${taskID} 租约已经失效：${String(error)}`)
          return
        }
        console.error(`AXIS 自动任务 ${taskID} 后台续期失败：${String(error)}`)
      }
    }
    const timer = setInterval(() => { void renew() }, heartbeatIntervalMs)
    activeLeases.set(sessionID, { taskID, executionToken, timer })
  }

  return {
    tool: {
    axis_automatic_task_start: tool({
      description: "请 AXIS 按队列顺序启动自动任务；每条任务各使用新的 OpenCode 会话和独立子目录。",
      args: {},
      async execute() {
        return JSON.stringify(await requestAxis("/api/v1/automatic-tasks/execution/start-local", {}))
      },
    }),
    axis_automatic_task_claim: tool({
      description: "领取当前最早的未执行任务；AXIS 启动的单项会话必须传入指定任务 ID。",
      args: {
        expected_task_id: tool.schema.string().optional().describe("AXIS 指定给当前独立工作目录的任务 ID"),
      },
      async execute(args, context) {
        const result = await requestAxis(
          "/api/v1/automatic-tasks/claim",
          { session_id: context.sessionID, expected_task_id: args.expected_task_id },
        ) as { task?: { id?: string, execution_token?: string } | null }
        if (result.task?.id && result.task.execution_token) {
          startLease(context.sessionID, result.task.id, result.task.execution_token)
        } else {
          stopLease(context.sessionID)
        }
        return JSON.stringify(result)
      },
    }),
    axis_automatic_task_finish: tool({
      description: "把当前 OpenCode 会话领取的自动任务标记为成功或失败；单项会话完成后立即结束。",
      args: {
        task_id: tool.schema.string().describe("axis_automatic_task_claim 返回的任务 ID"),
        execution_token: tool.schema.string().describe("axis_automatic_task_claim 返回的领取令牌"),
        status: tool.schema.string().describe("只允许 succeeded 或 failed"),
        summary: tool.schema.string().optional().describe("简短的执行结果或失败原因"),
      },
      async execute(args, context) {
        if (args.status !== "succeeded" && args.status !== "failed") {
          throw new Error("status 只允许 succeeded 或 failed")
        }
        if (args.status === "failed" && !args.summary?.trim()) {
          throw new Error("failed 状态必须提供明确失败原因")
        }
        const result = await requestAxis(
          `/api/v1/automatic-tasks/${encodeURIComponent(args.task_id)}/finish`,
          {
            session_id: context.sessionID,
            execution_token: args.execution_token,
            status: args.status,
            summary: args.summary || null,
          },
        )
        stopLease(context.sessionID, args.execution_token)
        return JSON.stringify(result)
      },
    }),
    axis_automatic_task_heartbeat: tool({
      description: "续期当前自动任务的执行租约；长任务开始后至少每五分钟调用一次。",
      args: {
        task_id: tool.schema.string().describe("axis_automatic_task_claim 返回的任务 ID"),
        execution_token: tool.schema.string().describe("axis_automatic_task_claim 返回的领取令牌"),
      },
      async execute(args, context) {
        const result = await requestAxis(
          `/api/v1/automatic-tasks/${encodeURIComponent(args.task_id)}/heartbeat`,
          { session_id: context.sessionID, execution_token: args.execution_token },
        )
        return JSON.stringify(result)
      },
    }),
    },
    event: async ({ event }) => {
      if (event.type === "session.deleted") stopLease(event.properties.info.id)
    },
    dispose: async () => {
      for (const lease of activeLeases.values()) clearInterval(lease.timer)
      activeLeases.clear()
    },
  }
}
