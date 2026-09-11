---
name: axis-video
description: 使用本机 AXIS 串行调度 RTX 4090 视频生成任务。用户说“使用场景切换技能生成视频”，或要求提交 ComfyUI API 工作流、后台监控、完成后回到当前 OpenCode 会话时使用。
---

# AXIS 视频生成

当用户说“使用场景切换技能生成视频”时，使用本 Skill，并在工作流准备完成后调用 `axis_video_submit`。

先准备可由 ComfyUI `/prompt` 接受的 API workflow JSON，并确保其中引用的本地素材路径已经存在。

提交前逐项检查：

- workflow JSON 可解析，其中引用的本地素材路径（输入图、参考视频、VAE、LoRA 等）都存在。
- 所有 `class_type` 节点在目标场景的 ComfyUI 中存在：查 `D:\AIWork\4090manager\config\control.json` 中场景的 `required_node_classes`，或对应实例的 `/object_info`（开发场景 3090 与视频场景 4090 同端口 127.0.0.1:8189、不同 cuda_device，同一安装目录、节点集合相同）。
- 用户需求含音频/BGM/音效时，工作流必须有音频解码与输出路径；不要提交纯视频工作流指望事后补音频。
- H3 工作流要模型原生音频（BGM/环境音）时不能只用 `VAEDecode`（视频 VAE，输出无声片）：改用 `MiniMaxH3AVDecodeT8`（av_latent + video_vae + audio_vae → frames + generated_audio），并把 `generated_audio` 接到 `CreateVideo` 的 `audio` 输入。ref2v 回填源片原音时仍用 `VAEDecode` + ffmpeg（见 h3-ref2v-video-pipeline skill）。
- 每个 H3 workflow JSON 只能包含一个生成采样分支。多段视频必须生成多个独立 JSON，并按顺序一次调用 `axis_video_submit_batch` 原子提交；禁止逐个预提交、并发调用单任务工具或向 AXIS 提交多分支 `batch_api.json`。

正常模式任务完成（AXIS 回传输出文件）后必须先验收再交付：

- `ffprobe` 校验流：用户要音频时确认音频流存在，且音视频流时长一致。
- 抽帧做视觉验收（每张截图独立子会话）。
- 缺流（如无声片）立即修正工作流重新提交，不要先交付再解释。

用户明确说“跳过检验”“跳过验收”“不要样片”或“直接正式生成”时启用免检快速模式：只做源片参数探测、安全分段、正式工作流构建、正式生成和必要的拼接/格式转换/音频回填；禁止生成任何 preview/sample，禁止抽帧、视觉验收、成片 `ffprobe` 检查和音频哈希检查，也不得后台补做。交付时明确标注“按用户要求未检验”。

调用 `axis_video_submit`：

- `workflow_path` 必须是现有 JSON 文件的绝对路径。
- 用户明确给出输出路径时传 `output_path`；否则省略，让 AXIS 使用默认输出目录。
- 用户明确指定生成场景名称时传 `scene_name`；否则省略，让 AXIS 使用场景设置中的默认生成场景。

工具会自动使用当前 OpenCode 会话和工作目录，不要索要或构造会话 ID、AXIS 密钥、OpenCode 用户名、密码或认证头。任务被接受后，告知用户可在 AXIS 的“视频任务”页面监控，然后结束当前响应；AXIS 生成完成后会恢复提交前的原场景，并把结果送回本会话。

单段任务调用 `axis_video_submit`。多段任务把全部正式单分支 JSON 按原顺序一次传给 `axis_video_submit_batch`；AXIS 会逐段释放生成显存，中间不回切场景也不回调，批尾统一恢复场景并只回调一次。不要把多个 H3 采样分支重新合并到同一工作流；已完成片段不得重复生成。

如果工具明确报错，原样说明错误原因。不要绕过 AXIS 直接切换场景或启动、停止 NInfer/ComfyUI。
