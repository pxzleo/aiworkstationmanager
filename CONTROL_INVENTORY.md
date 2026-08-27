# 正式控制配置现场盘点

> 盘点日期：2026-08-27（Asia/Singapore）
> 方式：现场只读；没有执行桌面启动脚本，没有调用 `start`、`stop`、`restart`、`up`、`down`，没有发送推理请求。
> 用途：保存实现前现场基线，并为已完成的正式配置实现提供可追溯输入。本文件不授予控制权限。

## 1. 结论与使用规则

根因已复核：`config/settings.lan.json` 指向不存在的 `config/control.json`；回退的 `config/control.example.json` 中所有环境均为 `configured:false`、`adapter:null`，所以 UI 按设计报告缺少适配器。[E-001]

正式控制实现已于同日按第 6 节补齐；表 2 的实现前现场证据仍有效，最新实现与启用状态以第 9 节为准。SenseVoiceSmall ASR 与 IndexTTS 1.5 vLLM 已作为 GPU AI 环境启用控制，其余 GPU AI 环境仍按各自缺口保持 Blocked。[E-006][E-007][E-013][E-015][E-016]

证据等级：`现场` 表示盘点轮只读命令/GET；`源码` 表示盘点时磁盘源文件或 unit；`建议` 表示实现前提出、当时尚非现有能力的方案。证据索引见第 8 节，当前实现状态见第 9 节。

## 2. 实现前现场基线状态表

本表是 2026-08-27 实现开始前的权威现场基线，保留“下一任务新增”“尚未实现”等历史时态以对应 E-001～E-014；它不代表实现后的代码能力。当前 adapter、probe、正式配置及 Ready/Blocked 结论以第 9 节和 E-015 为准。

| 环境 | 现场状态 | 唯一推荐生命周期映射 | 结论与阻断 | 证据 |
|---|---|---|---|---|
| `ninfer4090` | Compose `ninfer4090/ninfer` running；8080 在线 | `wsl_docker_compose`，`Ubuntu-22.04`，`/home/xu/ai_stud/ninfer4090`，project `ninfer4090`，service `ninfer` | **实现前 Blocked（适配器字段 Ready）**：当时代码缺 Prometheus drain，且现场缺可靠 WSL GPU 归属和受控显存预算 | [E-002][E-004][E-007] |
| `ninfer4090_ui` | user `ninfer-ui.service` active，PID 1258，8081 在线；unit 为 transient | `wsl_systemd`，scope `user`，service `ninfer-ui.service` | **Blocked**：必须先固化持久 user unit；这是非 GPU UI | [E-002][E-005][E-008] |
| `q27_4090` | system `q27-server.service` loaded、inactive/dead | 实现前建议下一任务新增 `wsl_systemd_root`，service `q27-server.service` | **实现前 Blocked**：桌面 start/stop 明确要求 `-u root`，盘点时代码的 `wsl_systemd` 没有该语义；另缺 drain/GPU 归属/显存预算 | [E-002][E-005][E-006][E-009] |
| `vllm4090` | `vllm-qwen38.service` not-found；8000 未监听 | 持久化后使用 `wsl_systemd`，scope `user`，service `vllm-qwen38.service` | **Blocked**：当前只由桌面脚本 `systemd-run --user` 临时创建；缺 Prometheus drain/GPU 归属/显存预算 | [E-002][E-005][E-010] |
| `ninfer3090` | Compose `ninfer3090/ninfer` exited；18030 未监听 | `wsl_docker_compose`，`Ubuntu-22.04`，`/home/xu/ai_stud/ninfer3090`，project `ninfer3090`，service `ninfer` | **Blocked（后端适配器字段 Ready）**：UI 必须拆分；缺 drain/GPU 归属和 24 GiB 预算验收 | [E-002][E-004][E-005] |
| 实现前建议新增 `ninfer3090_ui` | system `ninfer3090-ui.service` active，PID 207，18031 UI 在线；后端离线 | 实现前建议下一任务新增 `wsl_systemd_root`，service `ninfer3090-ui.service` | **实现前 Blocked**：盘点时适配器缺 `-u root`；应为 `gpu_ai:false` 的独立标准环境 | [E-002][E-005][E-008] |
| `dev3090_image` | 固定 Python ComfyUI，独立端口 8189，当前按需停止 | `windows_comfyui_process`；`--cuda-device 1`；独立 `user-image-3090` 数据库 | **Ready/configured**：固定 RTX 3090 UUID；Krea2 Turbo INT8 生图、MiniMax H3 pruned INT8 + 8-step LoRA 生视频；与 8001 音频实例隔离 | [E-012][E-018] |
| `dev3090_asr` | 独立 `sensevoice-asr-api.service` 已固化，当前 inactive；WSL 18090 未监听，Windows portproxy 保留 | `wsl_systemd` user unit；模型 `SenseVoiceSmall`；`CUDA_VISIBLE_DEVICES=1` | **Ready/configured**：严格 `/health` 身份、`/control/status` drain、3090 UUID/unit 绑定、模型路径、显存与 WSL 内部端口预检已接线 | [E-016] |
| `dev3090_tts` | `index-tts-vllm.service` 已改为 WSL `0.0.0.0:6006`，当前 inactive；Windows portproxy 保留 | `wsl_systemd` user unit；IndexTTS 1.5 vLLM；`CUDA_VISIBLE_DEVICES=1` | **Ready/configured**：严格 `/health` 服务/模型身份、voices、drain、3090 UUID/unit 绑定、模型路径、显存与 WSL 内部端口预检已接线 | [E-016] |
| `video_h3_4090` | ComfyUI 8000 未监听 | 实现前建议下一任务新增 `windows_comfyui_process`，port 8000、cuda device 0；桌面 GUI 快捷方式不是无人值守 adapter | **实现前 Blocked**：严格 Comfy/GPU/节点健康、Windows 生命周期、queue drain、Windows 路径检查当时尚未实现，显存预算尚未验收 | [E-002][E-003][E-012][E-013] |
| `video_aux_3090` | ComfyUI 8001 未监听 | 实现前建议下一任务新增 `windows_comfyui_process`，port 8001、cuda device 1 | **实现前 Blocked**：严格 Comfy/GPU/节点健康当时尚未实现；现场脚本 stop 为强制终止；当时缺 queue drain 联动和实际峰值显存预算 | [E-002][E-012][E-013] |
| 候选 `xiaozhi_server` | system `xiaozhi-server.service` active，PID 437，WSL 监听 `0.0.0.0:18000` | 实现前建议若控制整个 Xiaozhi 使用新增 `wsl_systemd_root`；不能作为 `dev3090_asr` adapter | **Blocked（非本次控制场景）**：启停会影响整套设备/WebSocket 会话；当前控制需求没有授权该生命周期 | [E-002][E-011] |

现场 GPU：4090 UUID `GPU-24e90667-f02e-1e21-e5fa-b4bd6566ce63`，49,140 MiB、最终复核时占用约 46,982 MiB；3090 UUID `GPU-3b71dd71-0d3f-6f92-8374-2f2b5f23ef8d`，24,576 MiB、计算显存 0 MiB。该数值仅是快照，不可直接转写为 `min_free_mib`。[E-004]

额外监听：3000 是 WSL 网易云音乐 API；18000 是 Xiaozhi 整体服务；8765 是无关的 Python 静态网页。它们都不是本次大模型/Comfy 控制入口，不得只凭端口接管。[E-002]

## 3. 每环境唯一推荐 health_check

以下对象来自实现前现场契约并已作为后续实现输入。对象可复制不等于环境可启用；当前是否 Ready 以第 9 节为准。对于尚无独立服务的环境，只能诚实使用 `adapter_status`，并明确它不足以证明模型能力。

| 环境 | 唯一推荐 JSON 对象 | 说明与证据 |
|---|---|---|
| `ninfer4090` | `{"type":"loopback_http","url":"http://127.0.0.1:8080/v1/models","expected_status":200,"json_equals":{"data.0.id":"qwen3.8-27b","data.0.modalities.vision":true},"timeout_seconds":5}` | 本轮现场 GET 匹配。[E-007] |
| `ninfer4090_ui` | `{"type":"loopback_http","url":"http://127.0.0.1:8081/api/snapshot","expected_status":200,"json_equals":{},"timeout_seconds":5}` | 仅证明 UI 路由在线；不把后端健康重复归到 UI。[E-008] |
| `q27_4090` | `{"type":"loopback_http","url":"http://127.0.0.1:8080/health","expected_status":200,"json_equals":{"status":"ok","model":"qwen38-27b-mtp-q6k.q27"},"timeout_seconds":5}` | 当前 unit 没有配置 API key，源码会返回非空 `model`；模型值由 ExecStart artifact basename 与源码赋值逻辑共同确定。服务本轮未启动。[E-009] |
| `vllm4090` | `{"type":"loopback_http","url":"http://127.0.0.1:8000/v1/models","expected_status":200,"json_equals":{"data.0.id":"qwen3.8-27b-fp8"},"timeout_seconds":5}` | served model name 来自桌面启动命令；服务本轮未启动。[E-010] |
| `ninfer3090` | `{"type":"loopback_http","url":"http://127.0.0.1:18030/v1/models","expected_status":200,"json_equals":{"data.0.id":"qwen3.8-27b","data.0.modalities.vision":true},"timeout_seconds":5}` | 来自当前 Compose 命令/模型配置；本轮后端停止，未 GET。[E-005] |
| `ninfer3090_ui` | `{"type":"loopback_http","url":"http://127.0.0.1:18031/api/snapshot","expected_status":200,"json_equals":{},"timeout_seconds":5}` | 本轮现场 GET 只证明 UI 在线；响应同时显示后端离线。[E-008] |
| `dev3090_image` | `{"type":"windows_comfy_capability_health","system_stats_url":"http://127.0.0.1:8189/system_stats","queue_url":"http://127.0.0.1:8189/queue","object_info_url":"http://127.0.0.1:8189/object_info","target_gpu_uuid":"GPU-3b71dd71-0d3f-6f92-8374-2f2b5f23ef8d","target_gpu_name":"NVIDIA GeForce RTX 3090","target_host_gpu_index":1,"expected_comfy_device_index":0,"required_node_classes":["UNETLoader","CLIPLoader","CLIPTextEncode","KSampler","VAELoader","VAEDecode","SaveImage","MiniMaxH3AudioConditioningT8","MiniMaxH3DualClockSamplerT8","MiniMaxH3AVDecodeT8","VHS_VideoCombine"],"timeout_seconds":5,"max_system_stats_bytes":65536,"max_queue_bytes":65536,"max_object_info_bytes":8388608}` | 8189 独立开发 Krea2/H3 实例；同时验证物理 3090、内部 cuda:0、生图加载/采样与 H3/VHS 视频节点能力。[E-018] |
| `dev3090_asr` | `{"type":"adapter_status"}` | 18000 只能证明 Xiaozhi 整体服务，不能证明独立 ASR；必须拆分后再增加能力健康探针。[E-011] |
| `dev3090_tts` | `{"type":"http_json_object_has_keys","url":"http://127.0.0.1:11996/audio/voices","required_keys":["jay_klee"],"expected_status":200,"timeout_seconds":5,"max_body_bytes":65536}` | 现接口 `/health` 没有 service/model/backend 身份；实际 `/audio/voices` 返回 object 而非 array，所以不能误用 `http_json_array_contains`。此类型验证至少一个现场存在的 voice capability，且必须与 `adapter_status` 对 `index-tts-vllm.service` 的 unit identity 一起使用；类型当前已实现，环境仍因 GPU 隔离、drain 和预算证据不足 Blocked。[E-011][E-015] |
| `video_h3_4090` | `{"type":"windows_comfy_capability_health","system_stats_url":"http://127.0.0.1:8000/system_stats","queue_url":"http://127.0.0.1:8000/queue","object_info_url":"http://127.0.0.1:8000/object_info","target_gpu_uuid":"GPU-24e90667-f02e-1e21-e5fa-b4bd6566ce63","target_gpu_name":"NVIDIA GeForce RTX 4090","target_host_gpu_index":0,"expected_comfy_device_index":0,"required_node_classes":["MiniMaxH3AudioConditioningT8","MiniMaxH3AVDecodeT8","MiniMaxH3DualClockSamplerT8"],"timeout_seconds":5,"max_system_stats_bytes":65536,"max_queue_bytes":65536,"max_object_info_bytes":8388608}` | 该类型同时验证物理 GPU 身份、queue 结构和 H3 节点能力；H3 8-step/LoRA/shift 仍由 `h3_video_profile` 独立静态预检。类型当前已实现，环境仍因无人值守与显存预算验收不足 Blocked。[E-004][E-012][E-013][E-015] |
| `video_aux_3090` | `{"type":"windows_comfy_capability_health","system_stats_url":"http://127.0.0.1:8001/system_stats","queue_url":"http://127.0.0.1:8001/queue","object_info_url":"http://127.0.0.1:8001/object_info","target_gpu_uuid":"GPU-3b71dd71-0d3f-6f92-8374-2f2b5f23ef8d","target_gpu_name":"NVIDIA GeForce RTX 3090","target_host_gpu_index":1,"expected_comfy_device_index":0,"required_node_classes":["TextEncodeAceStepAudio1.5","AILab_Qwen3TTSCustomVoice","AILab_Qwen3TTSVoiceDesign"],"timeout_seconds":5,"max_system_stats_bytes":65536,"max_queue_bytes":65536,"max_object_info_bytes":8388608}` | `--cuda-device 1` 会通过 `CUDA_VISIBLE_DEVICES=1` 将 3090 映射为 Comfy 内部 `cuda:0`，所以 host index=1、Comfy index=0 均需精确核对；同时验证 ACE-Step 与 Qwen3-TTS 节点。[E-004][E-012][E-013] |
| 候选 `xiaozhi_server` | `{"type":"loopback_tcp","host":"127.0.0.1","port":18000,"timeout_seconds":3}` | 只适用于“控制整个 Xiaozhi”场景，不得复用为 ASR 健康。[E-002][E-011] |

GPU AI 环境正式启用时还必须并列添加 `adapter_status` 和必要的模型/磁盘预检；上表刻意只给每环境唯一的能力健康对象，避免“任选其一”。`http_json_object_has_keys` 与 `windows_comfy_capability_health` 已进入 health 判别联合并有 mock 行为测试；对应环境仍因生命周期/预算证据不足保持 Blocked。[E-006][E-015]

## 4. 桌面与底层入口映射

`C:\Users\xu\Desktop\本地模型启动` 的 7 个 cmd/lnk 已完整只读解析，未执行。[E-003][E-005]

| 桌面入口 | 精确底层入口 | 端口/GPU | 日志或健康 |
|---|---|---|---|
| `4090-NInfer.cmd` | WSL `Ubuntu-22.04`；`/home/xu/ai_stud/ninfer4090`；Compose `ninfer4090/ninfer`；user `ninfer-ui.service` | 8080/8081；4090 | `/health`、`/v1/models`、`/slots`、`/metrics`；UI `/api/snapshot`；Compose logs/user journal |
| `4090-q27.cmd` | system `q27-server.service`；start/stop 显式 `-u root`；工作目录 `/home/xu/ai_stud/q27` | 8080；4090 | `/health`、`/stats`、`/v1/models`；system journal |
| `4090-vLLM.cmd` | user transient `vllm-qwen38.service`；模型 `/home/xu/models/Qwen3.8-27B-FP8`；`CUDA_VISIBLE_DEVICES=0` | 8000；4090 | `/health`、`/v1/models`、`/metrics`；`/home/xu/logs/vllm/qwen38-benchmark.log` |
| `3090-NInfer.cmd` | `/home/xu/ai_stud/ninfer3090`；Compose `ninfer3090/ninfer`；system `ninfer3090-ui.service` start/stop 显式 `-u root` | 18030/18031；Compose 固定 3090 UUID | NInfer 路由、UI `/api/snapshot`、Compose logs/system journal |
| `3090-Qwen3090-Control.cmd` | Windows Docker Compose `D:\AIWork\Test_MJ\qwen38-27b-rtx3090`，project `qwen38-27b-rtx3090`、service `single` | 18020；3090 | `/health`、Docker logs；不在当前环境清单 |
| `DualGPU-Llama-BF16.cmd` | Windows `D:\AIWork\qwen3.8_test\scripts\start-native-mtp.ps1`，原生 `llama-server.exe` | 1234；4090:3090=85:15 | `/health`、runtime logs；当前无对应 Windows adapter |
| `lmstudio监控.lnk` | `D:\AIWork\lmstudio_web_monitor\run.bat` → `start_lmstudio_web_monitor.ps1` | 8765；LM Studio API 1234 | 脚本含静态访问凭据，本文不记录；本轮 8765 owner 不是它 |

桌面根 `C:\Users\xu\Desktop\Comfy Desktop.lnk` 指向 `C:\Users\xu\AppData\Local\Programs\Comfy Desktop\Comfy Desktop.exe`，无参数。它是人工 GUI 生命周期边界，不应被当作无人值守进程适配器；下一任务应控制经审核的 Comfy Python 入口，而不是点击快捷方式。[E-003]

## 5. 现有自动预检能力与缺口

| 范围 | 当前可自动验证 | 仍缺少 |
|---|---|---|
| NInfer 4090/3090 | Compose 状态；loopback 模型 JSON；WSL 模型路径；GPU UUID/空闲显存数值 | Prometheus drain；可靠 WSL GPU owner；各卡受控冷启动峰值/稳定/释放预算 |
| q27 | unit status；二进制、模型、tokenizer 路径；`/health` 源码契约 | root adapter；`/stats` 无当前 active/queued 数值；GPU owner/预算 |
| vLLM | 模型目录、venv、日志目录、模型健康对象 | 持久 unit；Prometheus drain；GPU owner/预算 |
| IndexTTS | 独立 user unit；6006 `/health`、`/audio/voices`、`/tts`；3090 UUID/unit 绑定；活动请求 drain；Uvicorn 300 秒优雅关闭；显存与模型路径预算 | 当前未运行，尚未做本轮真实冷启动/推理验收 |
| SenseVoice ASR | 独立 user unit；18090 `/health`、OpenAI transcription；3090 UUID/unit 绑定；活动请求 drain；Uvicorn 300 秒优雅关闭；显存与模型路径预算 | 当前未运行，尚未做本轮真实冷启动/推理验收 |
| ComfyUI | `/system_stats` 与 `/queue` 路由约定；H3 静态 profile；8001 节点/模型文件 | Windows adapter；queue 数组映射；Windows 路径/磁盘探针；峰值显存 |

H3 唯一允许的静态 profile：[E-012]

- workflow：`C:\Users\xu\Documents\ComfyUI\user\default\workflows\api\openmontage_minimax_h3_local.json`
- `steps = 8`
- LoRA：`minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`
- `shift_video = 12.0`
- `shift_audio = 3.0`
- 主 diffusion：`minimax_h3_fl2va_pruned_int8_convrot.safetensors`
- 文本编码器：`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
- 视频/音频 VAE：`minimax_h3_video_vae_fp16.safetensors`、`minimax_h3_audio_vae_fp32.safetensors`

OpenMontage 路由固定为视频 8000、TTS/音乐 8001；Qwen3-TTS 与 ACE-Step 属于 8001 共享进程/队列，不应重复建两个可独立启停的环境。[E-012]

## 6. 已实现的字段契约

本节既是实现规格也是当前实现契约。所有新增模型均 `extra="forbid"`、冻结、使用 `type` 判别联合；所有外部命令固定参数、`shell=False`、stdin 关闭、隐藏窗口、输出先截断再脱敏。沿用限制：adapter 输出每流最多 8,192 字符，正式配置最多 256 KiB。[E-006][E-015]

### 6.1 `wsl_systemd_root`

判别名：`type: Literal["wsl_systemd_root"]`。

字段：

- `distro: str`：1..64，正则 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`。
- `service: str`：9..128，正则 `^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}\.service$`。
- `timeout_seconds: float`：1..120，默认 30。

固定参数数组：`["wsl.exe","-d",distro,"-u","root","--","systemctl",verb,service]`，`verb` 只能由 adapter 内部枚举 `is-active|start|stop|restart`，配置不得提供命令文本、user、额外参数或环境变量。q27 与 NInfer3090 UI 只能使用此类型，不能继续填写现有 `wsl_systemd(scope=system)`。[E-005][E-006]

状态语义与现有 systemd adapter 对齐：return 0 且 stdout 精确 `active` → `running`；stdout `inactive` 或 return 3 且非 `failed` → `stopped`；stdout `failed` → `failed`；其余、超时、不可解析 → `unknown`。动作非零抛显式 `adapter_action_failed`；超时抛 `adapter_timeout`，之后由控制面重新 status 对账，禁止静默判成功。[E-006]

### 6.2 `windows_comfyui_process`

判别名：`type: Literal["windows_comfyui_process"]`。只允许 Comfy Python 入口，不允许 `.cmd`、`.bat`、`.ps1`、桌面 `.lnk` 或任意 shell。

字段：

- `python_executable: str`、`main_path: str`、`working_directory: str`、`base_directory: str`、`user_directory: str`、`database_path: str`、`extra_model_paths_config: str`、`input_directory: str`、`output_directory: str`：规范化绝对 Windows 路径，各 3..512 字符；拒绝 NUL、相对路径、`..`、通配符；`python_executable` 后缀必须 `.exe`，`main_path` 文件名必须 `main.py`，`database_path` 后缀必须 `.db`，模型路径配置后缀必须 `.yaml|.yml`。
- `host: Literal["127.0.0.1"]`。
- `port: Literal[8000,8001,8189]`；8000 固定 GPU 0，8001 与开发场景独立端口 8189 固定 GPU 1。
- `cuda_device: Literal[0,1]`；8000 必须为 0，8001/8189 必须为 1。
- `startup_timeout_seconds: float`：1..120，默认 60；`stop_timeout_seconds: float`：1..120，默认 30。

固定启动数组：`[python_executable,"-s",main_path,"--base-directory",base_directory,"--user-directory",user_directory,"--database-url","sqlite:///"+database_path_as_forward_slashes,"--port",str(port),"--listen","127.0.0.1","--enable-manager","--cuda-device",str(cuda_device),"--extra-model-paths-config",extra_model_paths_config,"--input-directory",input_directory,"--output-directory",output_directory]`；cwd 固定为 `working_directory`，不得拼接任意 `extra_args`。这些字段覆盖现场 8000 Desktop 配置与 8001 启动脚本的实际参数，不需要执行原 PowerShell。启动后记录 PID、创建时间、规范化 exe/main/base/user/db/model-path/input/output/port/device 指纹，并以 `/system_stats` 成功作为 running 收敛条件。[E-012]

状态语义：PID 存活且 exe/命令行指纹全匹配且固定端口 owner 匹配 → `running`；PID 不存在且端口空闲 → `stopped`；PID/端口只匹配一部分、PID 被复用或存在陌生 owner → `unknown`，绝不接管。stop 只能在 queue drain=0 后针对记录的 PID/job object；超时返回失败并重新对账，不能按进程名批量杀死。Comfy Desktop GUI 保持人工边界。[E-003][E-012][E-013]

### 6.3 `drain_http_prometheus`

判别名：`type: Literal["drain_http_prometheus"]`，`purpose: Literal["active_requests"]`。

字段：

- `url: str`：10..512，只允许带固定端口/绝对 path、无凭据/query/fragment 的 loopback HTTP。
- `series: tuple[PrometheusSeries,...]`：1..8；每项 `{metric, labels}`。`metric` 1..128，正则 `^[A-Za-z_:][A-Za-z0-9_:]*$`；`labels` 0..8 个，键正则 `^[A-Za-z_][A-Za-z0-9_]*$`、值 0..256，必须精确匹配，禁止正则。
- `timeout_seconds: float` 0.1..10，默认 3；`wait_timeout_seconds: float` 1..600，默认 60；`poll_interval_seconds: float` 0.1..10，默认 1；`max_body_bytes: Literal[65536]`。

解析必须接受 Prometheus text 0.0.4，拒绝重复的完全相同 metric+labels、NaN、Inf、负数和非数值；每个登记 series 必须恰好匹配一次，缺失不能按 0。所有登记值同时等于数值 0 才 drain；HTTP 非 200、超限、解析错误均显式失败。

精确映射：

- NInfer 4090/3090 `/metrics`：`llamacpp:requests_processing{}` 与 `llamacpp:requests_deferred{}`；本轮现场均为 0，源码定义分别为 processing 与 `in_flight-processing`。[E-007]
- vLLM 4090 `/metrics`：`vllm:num_requests_running{model_name="qwen3.8-27b-fp8",engine="0"}` 与 `vllm:num_requests_waiting{model_name="qwen3.8-27b-fp8",engine="0"}`。当前安装源码固定 label names 为 `model_name,engine`。[E-010]

### 6.4 `drain_http_json_arrays`

判别名：`type: Literal["drain_http_json_arrays"]`，`purpose: Literal["comfy_queue"]`。

字段沿用现有 drain 的 URL/timeout/wait/poll 范围，并固定 `running_path: Literal["queue_running"]`、`pending_path: Literal["queue_pending"]`、`max_body_bytes: Literal[65536]`。

GET `/queue` 后必须验证顶层 JSON object，两个字段都存在且类型均为 array；映射 `running=len(queue_running)`、`pending=len(queue_pending)`。二者均为 0 才 drain。字段缺失、null、非数组、HTTP 非 200、响应超限或 JSON 错误一律失败，不能把缺失数组视为 0。[E-012][E-013]

### 6.5 严格能力健康与 Windows GPU 交叉类型

`http_json_object_has_keys` 加入 `HealthCheck` 判别联合，专用于现有 IndexTTS voices object：

- `type: Literal["http_json_object_has_keys"]`；`url` 10..512，沿用严格 loopback HTTP 规则。
- `required_keys: tuple[str,...]`：1..32、去重；每项 1..128，正则 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`。
- `expected_status: int` 100..599，默认 200；`timeout_seconds: float` 0.1..15，默认 5；`max_body_bytes: Literal[65536]`。
- 语义：响应必须是顶层 JSON object，每个 key 必须存在且对应 value 非 null；array/scalar、缺 key、重复配置、超限或解析失败均 `failed`。IndexTTS 还必须同时通过 `adapter_status`，从而把 voice capability 与 `index-tts-vllm.service` unit identity 绑定。更优的后续方案是在 `/health` 增加固定 `service/model/backend` 字段，但在真实端点实现前不得在配置中假写。[E-011]

`windows_comfy_capability_health` 同时是 Comfy 严格能力检查和 Windows GPU 交叉检查，加入 `HealthCheck` 判别联合：

- 三个 URL 字段 `system_stats_url`、`queue_url`、`object_info_url`：各 10..512，必须是相同 loopback host/port，path 分别精确 `/system_stats`、`/queue`、`/object_info`。
- `target_gpu_uuid: str`：完整 UUID，正则 `^GPU-[A-Fa-f0-9-]{16,64}$`，最大 68；`target_gpu_name: str`：1..128，现场只允许精确 `NVIDIA GeForce RTX 4090` 或 `NVIDIA GeForce RTX 3090`。
- `target_host_gpu_index: Literal[0,1]`；`expected_comfy_device_index: Literal[0]`。后者固定为 0，因为 `--cuda-device N` 在 Comfy `main.py:96-100` 设置 `CUDA_VISIBLE_DEVICES=N`，被选物理卡在进程内重新编号为 cuda:0。
- `required_node_classes: tuple[str,...]`：1..32、去重，每项 1..128，正则 `^[A-Za-z_][A-Za-z0-9_.-]{0,127}$`。
- `timeout_seconds: float` 0.1..15，默认 5；`max_system_stats_bytes: Literal[65536]`、`max_queue_bytes: Literal[65536]`、`max_object_info_bytes: int` 固定 8,388,608。

固定 GPU 交叉流程：先执行 `nvidia-smi --query-gpu=index,uuid,name --format=csv,noheader,nounits`，要求 host index、完整 UUID、名称唯一且三者精确匹配；再 GET `/system_stats`，要求顶层 `devices` 为非空 array，`devices[0].type="cuda"`、`devices[0].index=0`，且 `devices[0].name` 中解析出的厂商/型号精确等于 `target_gpu_name`。不得仅按 ordinal 判断。[E-004][E-012]

能力流程：`/queue` 必须是 object 且 `queue_running`/`queue_pending` 都是 array；`/object_info` 必须是 object 且每个 `required_node_classes` 都是顶层 key。H3 必须要求三个 MiniMaxH3 T8 节点，8001 必须要求 ACE-Step 1.5 和 Qwen3-TTS 节点；这只证明节点已加载，不发送 prompt。[E-012]

状态语义：所有 HTTP、GPU和能力证据完整且精确匹配 → `healthy`；证据完整但 UUID/名称/index/节点/JSON 类型不匹配 → `failed`；命令权限不足、超时、HTTP/JSON超限、设备或节点结果歧义 → `unknown`。`failed` 与 `unknown` 都禁止启动/场景切换；错误消息必须指出失败阶段，不能用万能异常吞掉根因。

### 6.6 `windows_path_disk`

判别名：`type: Literal["windows_path_disk"]`，加入 `PreflightCheck` 判别联合。

字段：

- `purpose: Literal["model","lora"]`。
- `path: str`：3..512；必须是盘符绝对路径 `^[A-Za-z]:\\`，经 `ntpath.normpath` 后必须与输入等价；拒绝相对路径、`..` path component、NUL、通配符、尾随空格/点，以及 UNC `\\server\share`、extended/device path `\\?\`、`\\.\`、`\??\`。
- `min_free_gib: int`：1..16,384；使用 GiB=1,073,741,824 bytes，不与十进制 GB 混用。
- `timeout_seconds: float`：1..30，默认 8。

实现使用可终止的隔离 Python/Win32 探针：逐级检查父目录与目标的 symlink/junction/reparse，再对所在卷调用 `GetDiskFreeSpaceExW`；`timeout_seconds` 实际强制，超时终止探针并返回 unknown。路径还拒绝 ADS 和 Win32 保留设备名。不得启动 PowerShell/cmd 或接受命令文本。路径存在且卷可用字节 `>= min_free_gib*2^30` → `healthy`；路径确定不存在或空间确定不足 → `failed`；卷解析、权限、Win32 API、reparse解析或超时无法得出可信结论 → `unknown`。`failed/unknown` 均阻断操作。[E-006][E-013][E-015]

## 7. 正式配置建议顺序

1. 保持 `control_enabled:false`，不要先创建表面完整的启用配置。
2. 实现并测试第 6 节全部强类型能力，包括严格 IndexTTS/Comfy 健康、Windows GPU 交叉与 `windows_path_disk`。
3. 固化 `ninfer-ui.service` 和 `vllm-qwen38.service`；把 NInfer3090 后端/UI 拆成两个环境。
4. 对每个 GPU AI 环境在用户明确授权后做一次受控冷启动，记录启动峰值、稳定显存、退出释放、健康 JSON、GPU UUID归属，才确定 `min_free_mib`。
5. 只有表 2 中对应环境全部阻断关闭后，才把该环境单独改为 `configured:true`；不要批量启用。

## 8. 证据索引

所有采集均为只读。命令中的路径为实际路径；摘要不包含密码、token、cookie 或私有配置正文。

| ID | 采集时间 | 精确只读命令或源文件 | 关键输出摘要 |
|---|---|---|---|
| E-001 | 2026-08-27 11:40 +08 | `Test-Path config\control.json`；`Get-Content config\settings.lan.json`；`Get-Content config\control.example.json` | 正式文件不存在；settings 指向它；示例 10 个环境均 disabled/null |
| E-002 | 2026-08-27 11:59:10 +08 | `Get-ScheduledTask -TaskName AXIS-AI-Workstation-Manager`；`Get-NetTCPConnection -State Listen`（仅筛选 3000/8000/8001/8080/8081/8765/11996/18000/18020/18030/18031/19100）；`wsl.exe -d Ubuntu-22.04 -- systemctl show q27-server.service ninfer3090-ui.service xiaozhi-server.service --no-pager --property=Id,LoadState,ActiveState,SubState,MainPID`；同样的 `systemctl --user show ninfer-ui.service vllm-qwen38.service index-tts-vllm.service` | 管理器任务 Running/PID 2848/19100；NInfer4090 8080、UI8081；q27 stopped；3090 UI18031；Xiaozhi18000；IndexTTS/vLLM/Comfy未监听 |
| E-003 | 2026-08-27 11:35 +08 | `Get-ChildItem C:\Users\xu\Desktop\本地模型启动 -File` 后对 `.cmd/.bat/.ps1` 逐文件 `Get-Content -Raw`；对 `.lnk` 使用 `WScript.Shell.CreateShortcut()` 读取 TargetPath/Arguments/WorkingDirectory；同法解析桌面根 `Comfy Desktop.lnk` | 7 个模型入口完整解析；Comfy快捷方式指向 `Comfy Desktop.exe` 且无参数；未执行 |
| E-004 | 2026-08-27 12:02 +08 | `wsl.exe -d Ubuntu-22.04 -- docker ps -a --filter name=ninfer --format '{{.Names}} {{.State}} {{.Status}}'`；`nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used --format=csv,noheader` | NInfer4090 running、NInfer3090 exited；host index/完整UUID/名称分别为 0/4090 与 1/3090；显存约46982 MiB/0 MiB |
| E-005 | 2026-08-27 11:45 +08 | `C:\Users\xu\Desktop\本地模型启动\4090-q27.cmd:49,87`；`3090-NInfer.cmd:69,111`；NInfer 两套 Compose `docker compose config`/`ps -a` | q27 与3090 UI start/stop 均显式 `-u root`；Compose project/service/path/端口/3090 UUID 确认 |
| E-006 | 2026-08-27 11:50 +08 | `workstation_manager/control.py:24-28,48-80,84-152,366-391,464-524,576-618,621-705` | 当前类型/范围、固定 args、状态语义、8,192字符输出限幅、64 KiB HTTP/drain限幅；system scope 未加 `-u root` |
| E-007 | 2026-08-27 11:52 +08 | `Invoke-RestMethod http://127.0.0.1:8080/health`；`Invoke-RestMethod http://127.0.0.1:8080/v1/models`；`Invoke-WebRequest http://127.0.0.1:8080/metrics`（只保留 request 指标）；`/home/xu/ai_stud/ninfer4090/src/serve/serve_metrics.cpp:63-76` | health status ok；model id qwen3.8-27b/vision true；processing=0、deferred=0；指标语义由源码确认 |
| E-008 | 2026-08-27 11:52 +08 | 限时 GET `http://127.0.0.1:8081/api/snapshot`、`http://127.0.0.1:18031/api/snapshot`；`systemctl [--user] show/cat` | 4090 UI HTTP在线且后端在线；3090 UI HTTP在线但后端离线；均为本轮即时 GET |
| E-009 | 2026-08-27 12:02 +08 | `wsl.exe -d Ubuntu-22.04 -- systemctl cat q27-server.service`；`/home/xu/ai_stud/q27/src/metal/metal_server.cpp:670,771,2269-2279,2290-2319` | unit ExecStart 的 artifact basename 为 `qwen38-27b-mtp-q6k.q27`且没有 API key；源码把 basename 写入 `model_name` 并在详细 `/health` 返回非空 model；未启动服务做 GET |
| E-010 | 2026-08-27 11:52 +08 | `C:\Users\xu\Desktop\本地模型启动\4090-vLLM.cmd:25,63,86`；`/home/xu/venvs/vllm/lib/python3.12/site-packages/vllm/v1/metrics/loggers.py:443-510` | 临时 user unit、8000、served name、CUDA ordinal；running/waiting 指标及 model_name/engine 标签确认 |
| E-011 | 2026-08-27 12:02 +08 | `wsl.exe -d Ubuntu-22.04 -- systemctl --user cat index-tts-vllm.service`；`wsl.exe -d Ubuntu-22.04 -- systemctl --user show index-tts-vllm.service --property=Id,LoadState,ActiveState,SubState,MainPID`；`wsl.exe -d Ubuntu-22.04 -- systemctl cat xiaozhi-server.service`；`wsl.exe -d Ubuntu-22.04 -- systemctl show xiaozhi-server.service --property=Id,LoadState,ActiveState,SubState,MainPID`；Windows `rg -n -e '@app.get\("/health"\)' -e '@app.get\("/audio/voices"\)' -e '@app.post\("/tts"\)' -e 'cuda:0' \\wsl.localhost\Ubuntu-22.04\home\xu\ai_stud\xz_server\index-tts-vllm -g '*.py'`；Windows `rg -n -e 'ASR: FunASR' -e 'model_dir: models/SenseVoiceSmall' \\wsl.localhost\Ubuntu-22.04\home\xu\ai_stud\xz_server\xiaozhi-esp32-server\main\xiaozhi-server\config.yaml`；`wsl.exe -d Ubuntu-22.04 -- journalctl -u xiaozhi-server.service --no-pager -n 300` 后在返回文本中只保留 ASR 初始化关键词行 | WSL 内未安装 rg，故源码搜索明确由 Windows rg 读取 UNC 根；voices 是包含 `jay_klee` 等 key 的 object，文件316 bytes；IndexTTS默认cuda:0；Xiaozhi候选/当前实际ASR及耦合确认 |
| E-012 | 2026-08-27 12:03 +08 | `rg -n -e 'system_stats' -e 'queue_running' -e 'queue_pending' -e 'object_info' -e 'NODE_CLASS_MAPPINGS' -e 'MiniMaxH3' -e 'Qwen3TTS' -e 'TextEncodeAceStepAudio1.5' C:\Users\xu\ComfyUI-Installs\ComfyUI\ComfyUI C:\Users\xu\Documents\ComfyUI D:\AIWork\openmontage -g '*.py' -g '*.json'`；并读取 `C:\Users\xu\ComfyUI-Installs\ComfyUI\ComfyUI\server.py:686-737`、`C:\Users\xu\ComfyUI-Installs\ComfyUI\ComfyUI\main.py:85-100`、`C:\Users\xu\Documents\ComfyUI\user\default\workflows\api\openmontage_minimax_h3_local.json:31,61-63,89`、`C:\Users\xu\Documents\ComfyUI\audio-3090\Start-ComfyUI-Audio-3090.ps1:3-49`、`C:\Users\xu\Documents\ComfyUI\audio-3090\Stop-ComfyUI-Audio-3090.ps1:4-14`、`D:\AIWork\openmontage\tools\_comfyui\workflows\ace-step-1.5-t2a.json:3-87` | 完整根和pattern已固定；system_stats devices结构、CUDA重编号、queue两数组、H3/ACE/Qwen节点、8000/8001参数与强制stop确认 |
| E-013 | 2026-08-27 11:50 +08 | `workstation_manager/control.py:100-152,576-705`；`nvidia-smi --query-compute-apps=gpu_uuid,process_name --format=csv,noheader,nounits`（Windows 与 WSL） | 现有 drain 只接受数值 JSON；Windows看WSL进程名权限不足、WSL内为 Not Found；Windows路径无现有 disk probe |
| E-014 | 2026-08-27 12:05 +08 | 回归：`python -m unittest discover -s tests -v`。文档 JSON 语法：`$t=Get-Content CONTROL_INVENTORY.md -Raw; $m=[regex]::Matches($t,'`(\{\"type\"[^`]+\})`'); foreach($x in $m){$null=ConvertFrom-Json -InputObject $x.Groups[1].Value}`。现有类型测试文件：`D:\AIWork\4090manager\tests\test_control.py`，方法 `AdapterTests.test_health_schema_is_loopback_only_and_gpu_command_is_fixed`；第 6.5 节新增类型在实现任务中必须补等价 schema/行为测试 | 141 项回归 OK；全部推荐对象是合法 JSON；新增类型尚未进入当前判别联合，因此对应环境保持 Blocked |
| E-015 | 2026-08-27（实现轮） | `workstation_manager/control.py`、`tests/test_control.py`、`config/control.json`；全部动作 runner/process/probe 使用 mock；正式配置仅调用 `load_control_config()` 解析 | root/Windows Comfy adapter 与五类探针已接线；正式配置 11 个环境可解析，只有 `ninfer3090_ui` 为 configured；全回归、Node 与 compileall 见本轮最终验收 |
| E-016 | 2026-08-27（语音服务接入轮） | 只读核对 Windows `Get-NetTCPConnection`/`netsh interface portproxy`、WSL `ss -ltnp`、两个源码目录/模型/venv；安装后执行 `systemctl --user daemon-reload` 与 `systemctl --user show sensevoice-asr-api.service index-tts-vllm.service --property=Id,LoadState,ActiveState,UnitFileState,Environment` | 18090/6006 Windows 转发存在而 WSL 服务均停止；两个 user unit 均 loaded、inactive、disabled 且唯一固定 `CUDA_VISIBLE_DEVICES=1`；未在本轮自动启动模型 |
| E-017 | 2026-08-27（NInfer4090 安全验收轮） | 请求指标清零后 `docker compose up -d --force-recreate ninfer`；`docker inspect`；容器内固定 `cuda-visible-probe`；`/health`、`/v1/models`、真实 `/v1/chat/completions`、UI `/api/snapshot`；`systemctl --user show/is-enabled ninfer-ui.service` | DeviceRequests、NVIDIA/CUDA env 与 CUDA Driver 均唯一为 4090 UUID；Driver `count=1`；冷启动门槛 47000 MiB；restart=no、StopTimeout=600；模型 qwen3.8-27b/vision=true、真实请求返回“验收通过”；UI unit persistent/enabled/active，旧 LAN IP 已移除 |
| E-018 | 2026-08-27（ComfyUI 3090 开发图像/视频接入轮） | `installations.json`、固定 Python/main.py、历史 8189 日志、Krea2/H3 工作流、模型目录与节点源码；正式配置；真实冷启动 `127.0.0.1:8189/system_stats|queue|object_info`；回归测试 | 独立 8189/user-image-3090；`--cuda-device 1`；启动日志与 API 均确认 `NVIDIA GeForce RTX 3090`、内部 cuda:0、24 GiB，11 个 Krea2/H3/VHS 必需节点无缺失且队列为空；Krea2/H3 全套启用模型文件、H3 8-step profile、host index 1 与 3090 UUID 交叉核对及 12 GiB 冷启动余量列入预检；Krea2 模板中的可选 darkbrush LoRA 开关为 false，未作为必需模型 |

## 9. 实现后正式配置状态

`config/control.json` 已创建且 `control_enabled:true`。`ninfer4090`、`ninfer4090_ui`、`ninfer3090_ui`、`dev3090_image`、`dev3090_asr` 与 `dev3090_tts` 为 **Ready/configured** 环境。开发 Krea2/H3 ComfyUI 使用独立 8189/user-image-3090、固定 `--cuda-device 1`、3090 UUID、Krea2/H3/VHS 节点健康、两套模型全路径、H3 8-step profile 与显存/端口预检。NInfer4090 使用 Prometheus drain、4090 UUID 显存预算、模型路径/磁盘、端口及 WSL Docker Compose/CUDA Driver 四层绑定检查；UI 使用持久 enabled user unit。ASR/TTS 使用独立 WSL user unit、18090/6006、数值 drain、3090 UUID/unit 绑定、显存预算、模型路径、WSL 内部端口预检，并在 unit active 后最多 600 秒轮询严格模型健康。其余 GPU 环境继续按各自证据保持 **Blocked**，已登记 adapter 的 Blocked 环境仍只读显示真实状态。

AI/Comfy 的 `restart` 与 `stop` 使用匹配服务类型的强类型 drain 门槛：NInfer/vLLM 使用 Prometheus，Comfy 使用两个 queue 数组，ASR/TTS 使用 `/control/status` 的 `active_requests` 数值；失败/超时绝不调用 adapter。ASR/TTS 随后的 Uvicorn SIGTERM 优雅关闭会停止接收新连接，并在 300 秒内等待竞态在途请求；systemd `TimeoutStopSec=330`，管理器适配器 `timeout_seconds=630`，不会先于 unit 超时。[E-015][E-016]

development 场景继续把 NInfer4090 后端/UI、3090 ComfyUI 图像/视频、ASR、TTS 全部列为 `desired` 必需项，现均已具备固定适配器和动作能力；具体视频模型与三类 3090 模型同时驻留的峰值显存仍按实际工作流验收。`optional_desired` schema 保留但本场景不使用。video 仍以两套实际 ComfyUI 为必需项。

历史只读基线仍见表 2/E-002/E-004，旧基线回归见 [E-014]，实现后验证见 [E-015]，语音接入见 [E-016]，NInfer4090 受控重建与真实推理验收见 [E-017]。
