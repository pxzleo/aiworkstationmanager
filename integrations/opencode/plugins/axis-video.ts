import { createHash } from "node:crypto"
import { type Plugin, tool } from "@opencode-ai/plugin"

const AXIS_BASE_URL = "http://127.0.0.1:19100"

export const AxisVideoPlugin: Plugin = async ({ client }) => {
  const callback = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      if (request.method !== "POST") return new Response("Not found", { status: 404 })
      const url = new URL(request.url)
      const match = /^\/session\/([^/]+)\/prompt_async$/.exec(url.pathname)
      if (!match) return new Response("Not found", { status: 404 })

      let body: { parts?: unknown[] }
      try {
        body = await request.json() as { parts?: unknown[] }
      } catch (error) {
        return new Response(`Invalid JSON: ${String(error)}`, { status: 400 })
      }
      if (!Array.isArray(body.parts)) {
        return new Response("parts must be an array", { status: 400 })
      }

      let sessionID: string
      try {
        sessionID = decodeURIComponent(match[1])
      } catch (error) {
        return new Response(`Invalid session ID: ${String(error)}`, { status: 400 })
      }
      const directory = url.searchParams.get("directory") || undefined
      const result = await client.session.promptAsync({
        path: { id: sessionID },
        query: directory ? { directory } : undefined,
        body: { parts: body.parts as never[] },
      })
      if (result.error) {
        return Response.json(result.error, { status: 502 })
      }
      return new Response(null, { status: 204 })
    },
  })

  async function submitToAxis(path: string, payload: unknown): Promise<unknown> {
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
    if (!response.ok) throw new Error(`AXIS 拒绝视频任务（HTTP ${response.status}）：${text}`)
    try {
      return JSON.parse(text)
    } catch (error) {
      throw new Error(`AXIS 返回了无效 JSON：${text}`, { cause: error })
    }
  }

  return {
    tool: {
      axis_video_submit: tool({
        description: "使用场景切换技能生成视频：将已准备好的 ComfyUI API 工作流提交给本机 AXIS，自动绑定当前 OpenCode 会话并异步回传结果。",
        args: {
          workflow_path: tool.schema.string().describe("仅包含一个 H3 生成分支的 ComfyUI API workflow JSON 绝对路径；多段视频需拆成多个任务，由 AXIS 串行执行"),
          output_path: tool.schema.string().optional().describe("可选的绝对输出文件路径；省略时使用 AXIS 默认目录"),
          scene_name: tool.schema.string().optional().describe("可选的生成场景名称；省略时使用 AXIS 默认生成场景"),
        },
        async execute(args, context) {
          const identity = JSON.stringify({
            sessionID: context.sessionID,
            messageID: context.messageID,
            workflowPath: args.workflow_path,
            outputPath: args.output_path || null,
            sceneName: args.scene_name || null,
          })
          const idempotencyKey = `opencode-${createHash("sha256").update(identity).digest("hex")}`
          const payload = {
            idempotency_key: idempotencyKey,
            session_id: context.sessionID,
            workflow_path: args.workflow_path,
            output_path: args.output_path || null,
            scene_name: args.scene_name || null,
            callback_url: `http://127.0.0.1:${callback.port}`,
            callback_directory: context.directory,
          }
          const result = await submitToAxis("/api/v1/video-jobs", payload) as {
            job?: { id?: string; status?: string }; created?: boolean
          }
          const jobID = result.job?.id
          if (!jobID) throw new Error("AXIS 响应缺少视频任务 ID")
          return `AXIS 视频任务已${result.created ? "创建" : "存在"}：${jobID}，状态 ${result.job?.status || "unknown"}。可在 http://127.0.0.1:19100 的“视频任务”页面监控；完成后结果会自动回到当前会话。`
        },
      }),
      axis_video_submit_batch: tool({
        description: "将多个单分支 H3 工作流作为一个有序批次原子提交给 AXIS；批内逐段释放显存，批尾恢复场景并只回传一次。",
        args: {
          workflow_paths: tool.schema.array(tool.schema.string()).min(1).max(100).describe("按生成顺序排列的 ComfyUI API workflow JSON 绝对路径"),
          output_paths: tool.schema.array(tool.schema.string()).optional().describe("可选的对应绝对输出路径数组，数量必须与 workflow_paths 一致"),
          scene_name: tool.schema.string().optional().describe("可选的生成场景名称；省略时使用 AXIS 默认生成场景"),
        },
        async execute(args, context) {
          if (args.output_paths && args.output_paths.length !== args.workflow_paths.length) {
            throw new Error("output_paths 数量必须与 workflow_paths 一致")
          }
          const identity = JSON.stringify({
            sessionID: context.sessionID,
            messageID: context.messageID,
            workflowPaths: args.workflow_paths,
            outputPaths: args.output_paths || null,
            sceneName: args.scene_name || null,
          })
          const payload = {
            idempotency_key: `opencode-batch-${createHash("sha256").update(identity).digest("hex")}`,
            session_id: context.sessionID,
            workflows: args.workflow_paths.map((workflowPath, index) => ({
              workflow_path: workflowPath,
              output_path: args.output_paths?.[index] || null,
            })),
            scene_name: args.scene_name || null,
            callback_url: `http://127.0.0.1:${callback.port}`,
            callback_directory: context.directory,
          }
          const result = await submitToAxis("/api/v1/video-job-batches", payload) as {
            jobs?: Array<{ id?: string; status?: string }>; batch_id?: string; created?: boolean
          }
          if (!result.batch_id || !result.jobs?.length) {
            throw new Error("AXIS 响应缺少视频批次 ID 或任务列表")
          }
          return `AXIS 视频批次已${result.created ? "创建" : "存在"}：${result.batch_id}，共 ${result.jobs.length} 段。可在 http://127.0.0.1:19100 的“视频任务”页面监控；批次完成或失败后只回传一次。`
        },
      }),
    },
    dispose: async () => callback.stop(true),
  }
}
