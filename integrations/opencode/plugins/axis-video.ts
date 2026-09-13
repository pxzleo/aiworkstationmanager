import { createHash } from "node:crypto"
import { createReadStream } from "node:fs"
import { readFile } from "node:fs/promises"
import { basename, isAbsolute, resolve } from "node:path"
import { type Plugin, tool } from "@opencode-ai/plugin"

const AXIS_BASE_URL = "http://127.0.0.1:19100"

type EvidenceRecord = { path?: unknown; sha256?: unknown }

async function sha256File(path: string): Promise<string> {
  return await new Promise((resolveHash, reject) => {
    const hash = createHash("sha256")
    const stream = createReadStream(path)
    stream.on("data", (chunk) => hash.update(chunk))
    stream.on("error", reject)
    stream.on("end", () => resolveHash(hash.digest("hex")))
  })
}

async function verifyEvidenceRecord(
  label: string,
  record: EvidenceRecord,
  expectedPath?: string,
): Promise<string> {
  if (typeof record?.path !== "string" || !isAbsolute(record.path)) {
    throw new Error(`${label} 证据路径必须是绝对路径`)
  }
  const path = resolve(record.path)
  if (expectedPath && path !== resolve(expectedPath)) {
    throw new Error(`${label} 证据路径与提交文件不一致`)
  }
  if (typeof record.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(record.sha256)) {
    throw new Error(`${label} 证据缺少有效 SHA-256`)
  }
  let actualHash: string
  try {
    actualHash = await sha256File(path)
  } catch (error) {
    throw new Error(`${label} 证据文件无法读取：${path}`, { cause: error })
  }
  if (actualHash !== record.sha256) throw new Error(`${label} 证据 SHA-256 不匹配：${path}`)
  return path
}

export async function verifyWorkflowEvidence(workflowPath: string): Promise<string | null> {
  if (!isAbsolute(workflowPath)) throw new Error("workflow_path 必须是绝对路径")
  let workflow: Record<string, { class_type?: unknown }>
  try {
    workflow = JSON.parse(await readFile(workflowPath, "utf8"))
  } catch (error) {
    throw new Error(`无法读取或解析 workflow：${workflowPath}`, { cause: error })
  }
  const isH3 = Object.values(workflow).some((node) =>
    typeof node?.class_type === "string"
    && node.class_type.startsWith("MiniMaxH3")
    && node.class_type.endsWith("ToVideo"),
  )
  if (!isH3) return null

  const evidencePath = `${workflowPath}.evidence.json`
  let evidence: {
    version?: unknown
    task_id?: unknown
    workflow?: EvidenceRecord
    source?: EvidenceRecord
    detection_report?: EvidenceRecord
    prompt?: EvidenceRecord
  }
  try {
    evidence = JSON.parse(await readFile(evidencePath, "utf8"))
  } catch (error) {
    throw new Error(`H3 workflow 缺少有效证据文件：${evidencePath}`, { cause: error })
  }
  if (evidence.version !== 1 || typeof evidence.task_id !== "string" || !evidence.task_id.trim()) {
    throw new Error(`H3 workflow 证据版本或任务 ID 无效：${evidencePath}`)
  }
  await verifyEvidenceRecord("workflow", evidence.workflow || {}, workflowPath)
  const sourcePath = await verifyEvidenceRecord("源片", evidence.source || {})
  const reportPath = await verifyEvidenceRecord("检测报告", evidence.detection_report || {})
  await verifyEvidenceRecord("提示词", evidence.prompt || {})
  if (basename(reportPath) !== "source_detection_report.json") {
    throw new Error("检测报告固定文件名必须是 source_detection_report.json")
  }
  let report: {
    task_id?: unknown
    source?: EvidenceRecord
    prompt_sha256?: unknown
    frames?: Array<EvidenceRecord & { agent_id?: unknown }>
  }
  try {
    report = JSON.parse(await readFile(reportPath, "utf8"))
  } catch (error) {
    throw new Error(`无法解析检测报告：${reportPath}`, { cause: error })
  }
  if (report.task_id !== evidence.task_id
      || resolve(String(report.source?.path || "")) !== sourcePath
      || report.source?.sha256 !== evidence.source?.sha256
      || report.prompt_sha256 !== evidence.prompt?.sha256) {
    throw new Error("检测报告与任务、源片或提示词证据不一致")
  }
  if (!Array.isArray(report.frames) || !report.frames.length) {
    throw new Error("检测报告缺少截图记录")
  }
  const agentIDs = new Set<string>()
  for (const [index, frame] of report.frames.entries()) {
    await verifyEvidenceRecord(`截图 ${index + 1}`, frame)
    const agentID = typeof frame.agent_id === "string" ? frame.agent_id.trim() : ""
    if (!agentID || agentIDs.has(agentID)) throw new Error("检测报告的子代理会话 ID 无效或重复")
    agentIDs.add(agentID)
  }
  return String(evidence.workflow?.sha256)
}

export const AxisVideoPlugin: Plugin = async ({ client }) => {
  const handoffReady = new Map<string, { sessionID: string; ready: boolean; revision: number }>()
  const callback = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      const url = new URL(request.url)
      const handoffMatch = /^\/session\/([^/]+)\/handoff_ready\/([^/]+)$/.exec(url.pathname)
      if (request.method === "GET" && handoffMatch) {
        let sessionID: string
        let handoffOwner: string
        try {
          sessionID = decodeURIComponent(handoffMatch[1])
          handoffOwner = decodeURIComponent(handoffMatch[2])
        } catch (error) {
          return new Response(`Invalid handoff identity: ${String(error)}`, { status: 400 })
        }
        const state = handoffReady.get(handoffOwner)
        if (!state || state.sessionID !== sessionID) return new Response("Not ready", { status: 425 })
        return new Response(null, { status: state.ready ? 204 : 425 })
      }
      if (request.method !== "POST") return new Response("Not found", { status: 404 })
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
      const handoffOwner = url.searchParams.get("handoff_owner")
      const previousReadiness = new Map<string, { ready: boolean; blockedRevision: number }>()
      if (handoffOwner) {
        for (const [owner, state] of handoffReady) {
          if (state.sessionID === sessionID && owner !== handoffOwner) {
            const ready = state.ready
            state.ready = false
            state.revision += 1
            previousReadiness.set(owner, { ready, blockedRevision: state.revision })
          }
        }
      }
      const restoreReadiness = () => {
        for (const [owner, previous] of previousReadiness) {
          const state = handoffReady.get(owner)
          if (state?.sessionID === sessionID && state.revision === previous.blockedRevision) {
            state.ready = previous.ready
            state.revision += 1
          }
        }
      }
      let result
      try {
        result = await client.session.promptAsync({
          path: { id: sessionID },
          query: directory ? { directory } : undefined,
          body: { parts: body.parts as never[] },
        })
      } catch (error) {
        restoreReadiness()
        throw error
      }
      if (result.error) {
        restoreReadiness()
        return Response.json(result.error, { status: 502 })
      }
      if (handoffOwner) handoffReady.delete(handoffOwner)
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
          const workflowSHA256 = await verifyWorkflowEvidence(args.workflow_path)
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
            workflow_file_sha256: workflowSHA256,
            output_path: args.output_path || null,
            scene_name: args.scene_name || null,
            callback_url: `http://127.0.0.1:${callback.port}`,
            callback_directory: context.directory,
          }
          let result: { job?: { id?: string; status?: string }; created?: boolean }
          try {
            result = await submitToAxis("/api/v1/video-jobs", payload) as typeof result
          } catch (error) {
            throw error
          }
          const jobID = result.job?.id
          if (!jobID) throw new Error("AXIS 响应缺少视频任务 ID")
          handoffReady.set(jobID, { sessionID: context.sessionID, ready: false, revision: 0 })
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
          const workflowHashes = []
          for (const workflowPath of args.workflow_paths) {
            workflowHashes.push(await verifyWorkflowEvidence(workflowPath))
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
              workflow_file_sha256: workflowHashes[index],
              output_path: args.output_paths?.[index] || null,
            })),
            scene_name: args.scene_name || null,
            callback_url: `http://127.0.0.1:${callback.port}`,
            callback_directory: context.directory,
          }
          let result: {
            jobs?: Array<{ id?: string; status?: string }>; batch_id?: string; created?: boolean
          }
          try {
            result = await submitToAxis("/api/v1/video-job-batches", payload) as typeof result
          } catch (error) {
            throw error
          }
          if (!result.batch_id || !result.jobs?.length) {
            throw new Error("AXIS 响应缺少视频批次 ID 或任务列表")
          }
          handoffReady.set(
            result.batch_id,
            { sessionID: context.sessionID, ready: false, revision: 0 },
          )
          return `AXIS 视频批次已${result.created ? "创建" : "存在"}：${result.batch_id}，共 ${result.jobs.length} 段。可在 http://127.0.0.1:19100 的“视频任务”页面监控；批次完成或失败后只回传一次。`
        },
      }),
    },
    event: async ({ event }) => {
      if (event.type === "session.idle" || event.type === "session.status") {
        const ready = event.type === "session.idle" || event.properties.status.type === "idle"
        for (const state of handoffReady.values()) {
          if (state.sessionID === event.properties.sessionID) {
            state.ready = ready
            state.revision += 1
          }
        }
      }
    },
    dispose: async () => callback.stop(true),
  }
}
