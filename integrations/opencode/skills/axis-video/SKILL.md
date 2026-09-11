---
name: axis-video
description: 使用本机 AXIS 串行调度 RTX 4090 视频生成任务。用户说“使用场景切换技能生成视频”，或要求提交 ComfyUI API 工作流、后台监控、完成后回到当前 OpenCode 会话时使用。
---

# AXIS 视频生成

当用户说“使用场景切换技能生成视频”时，使用本 Skill，并在工作流准备完成后调用 `axis_video_submit`。

先准备可由 ComfyUI `/prompt` 接受的 API workflow JSON，并确保其中引用的本地素材路径已经存在。

调用 `axis_video_submit`：

- `workflow_path` 必须是现有 JSON 文件的绝对路径。
- 用户明确给出输出路径时传 `output_path`；否则省略，让 AXIS 使用默认输出目录。

工具会自动使用当前 OpenCode 会话和工作目录，不要索要或构造会话 ID、AXIS 密钥、OpenCode 用户名、密码或认证头。任务被接受后，告知用户可在 AXIS 的“视频任务”页面监控，然后结束当前响应；AXIS 完成场景恢复后会把结果送回本会话。

如果工具明确报错，原样说明错误原因。不要绕过 AXIS 直接切换场景或启动、停止 NInfer/ComfyUI。
