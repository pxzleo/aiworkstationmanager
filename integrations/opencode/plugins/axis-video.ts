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

  return {
    tool: {
      axis_video_submit: tool({
        description: "使用场景切换技能生成视频：将已准备好的 ComfyUI API 工作流提交给本机 AXIS，自动绑定当前 OpenCode 会话并异步回传结果。",
        args: {
          workflow_path: tool.schema.string().describe("现有 ComfyUI API workflow JSON 的绝对路径"),
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
          let response: Response
          try {
            response = await fetch(`${AXIS_BASE_URL}/api/v1/video-jobs`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload),
            })
          } catch (error) {
            throw new Error(`无法连接 AXIS ${AXIS_BASE_URL}: ${String(error)}`, { cause: error })
          }
          const text = await response.text()
          if (!response.ok) {
            throw new Error(`AXIS 拒绝视频任务（HTTP ${response.status}）：${text}`)
          }
          let result: { job?: { id?: string; status?: string }; created?: boolean }
          try {
            result = JSON.parse(text)
          } catch (error) {
            throw new Error(`AXIS 返回了无效 JSON：${text}`, { cause: error })
          }
          const jobID = result.job?.id
          if (!jobID) throw new Error("AXIS 响应缺少视频任务 ID")
          return `AXIS 视频任务已${result.created ? "创建" : "存在"}：${jobID}，状态 ${result.job?.status || "unknown"}。可在 http://127.0.0.1:19100 的“视频任务”页面监控；完成后结果会自动回到当前会话。`
        },
      }),
    },
    dispose: async () => callback.stop(true),
  }
}
