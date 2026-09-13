# AXIS 开发文档

[English](DEVELOPMENT.en.md) | 简体中文

本文档面向需要开发、集成或排查 AXIS 的人员。普通安装和使用请先阅读 [README.zh-CN.md](README.zh-CN.md)，服务脚本协议见 [scriptspec.md](scriptspec.md)。

## 开发环境

以下安装和测试命令只适用于包含 `requirements-dev.txt` 和 `tests/` 的源码检出，不适用于发布包：

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test tests/frontend_request_guard.test.js tests/frontend_gpu_layout.test.js tests/frontend_monitor_chart.test.js tests/frontend_theme.test.js tests/frontend_contract.test.js tests/frontend_i18n.test.js tests/documentation_consistency.test.js
```

发布包不包含 `requirements-dev.txt` 或 `tests/`，其中的开发文档仅供接口和部署参考。生成干净发布目录：

```powershell
.\Build-Release.ps1 -Destination D:\Release\axis-manager
```

## 版本管理

`workstation_manager/__init__.py` 中的 `__version__` 是程序版本的唯一来源，健康接口和系统设置页面均读取该值。每次交付代码提交都必须按语义化版本规则递增版本号：兼容修正递增补丁版本，新增兼容功能递增次版本，不兼容变更递增主版本。

## API 约定

API 前缀为 `/api/v1`，请求和响应使用 JSON。错误响应保留稳定的 `error.code`，并根据 `Accept-Language` 返回中文或英文错误信息。

首次初始化的 `auth/setup` 只允许从本机 loopback 地址访问。前端确认会话状态期间只显示 AXIS 启动画面，不显示登录表单；仅在确认未登录或检查失败后显示登录界面。初始化完成后，除健康、认证状态和登录外，读取接口需要登录；除 `auth/setup` 和 `auth/login` 外的写操作（包括 `auth/logout`）还需要把当前会话令牌放入 `X-CSRF-Token` 请求头。会话 Cookie 为 `HttpOnly` 和 `SameSite=Strict`。

成功响应直接返回 JSON 对象，创建接口返回 `201`，异步动作返回 `202`，删除接口返回无正文的 `204`，其他成功接口返回 `200`。错误统一为 `{"error":{"code":"...","message":"...","details":...}}`；参数校验错误返回 `422`，未登录返回 `401`，CSRF 或访问来源不符合要求返回 `403`，目标不存在返回 `404`，冲突或已有操作执行中返回 `409`。

### 请求体

认证设置、登录和新增用户使用同一请求体。`username` 去除首尾空白后必须为 3..64 个字符，`password` 为 4..1024 个字符：

```json
{"username":"admin","password":"1234"}
```

只有登录接口可额外提交布尔字段 `remember`。为 `true` 时创建 30 天服务端会话并设置同期限的持久 Cookie；省略或为 `false` 时继续使用 `session_ttl_seconds`：

```json
{"username":"admin","password":"1234","remember":true}
```

修改密码使用：

```json
{"password":"new-password"}
```

登记或修改服务时必须提交完整对象；`description`、`gpu_label`、`ui_url` 可为空，`port` 可为 `null`：

```json
{
  "name": "3090 ComfyUI",
  "description": "图像生成服务",
  "script_path": "C:\\Services\\comfyui.ps1",
  "gpu_label": "RTX 3090",
  "port": 8189,
  "ui_url": "http://192.168.100.190:8189/",
  "wsl_portproxy_enabled": false,
  "wsl_distro": "Ubuntu-22.04",
  "wsl_listen_address": "0.0.0.0",
  "wsl_listen_port": null,
  "wsl_connect_port": null
}
```

`name` 最长 100 字符，`description` 最长 1000 字符，`script_path` 必须是现有 `.ps1`、`.cmd` 或 `.bat` 绝对路径，`gpu_label` 最长 100 字符，`port` 为 `1..65535`，`ui_url` 必须为空或完整 HTTP/HTTPS 地址。启用 `wsl_portproxy_enabled` 后，管理器启动及任一服务启动/重启前会统一校准全部已登记的 Windows `portproxy`，关闭、修改或删除登记时会清理旧映射；管理器仅更新或清理自己上次成功同步过的目标，未知现有映射会拒绝操作。同步成功还要求 IP Helper 实际持有监听端口。监听地址只允许 `0.0.0.0`、loopback 或私网 IPv4，监听端口为空时使用服务端口，WSL 目标端口为空时使用监听端口。

登记或修改场景使用有序且不包含未知服务的 `service_ids`；重复 ID 会按首次出现去重。`description` 是最长 1000 字符的卡片简短介绍，`detailed_description` 是最长 8000 字符的独立详细使用说明。`is_default_generation` 表示默认生成场景，最多只能有一个，由用户在场景编辑器中勾选：

```json
{"name":"视频生成","description":"视频服务组","detailed_description":"ComfyUI：http://127.0.0.1:8189","is_default_generation":true,"service_ids":["服务ID1","服务ID2"]}
```

场景排序的 `scene_ids` 必须恰好包含全部现有场景 ID 且不得重复：

```json
{"scene_ids":["场景ID1","场景ID2"]}
```

单服务动作请求为 `{"action":"start"}`，`action` 只允许 `start`、`stop`、`restart`。其他 POST/DELETE 动作不需要请求体。

### 健康与认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 管理器健康状态 |
| GET | `/api/v1/auth/status` | 是否完成初始化及当前认证状态 |
| POST | `/api/v1/auth/setup` | 创建首个管理员，仅限本机首次调用 |
| POST | `/api/v1/auth/login` | 登录并取得 CSRF 令牌 |
| POST | `/api/v1/auth/logout` | 注销当前会话 |
| GET | `/api/v1/auth/me` | 当前用户及刷新后的 CSRF 令牌 |

### 用户与资源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/users` | 用户列表 |
| POST | `/api/v1/users` | 新增用户，仅允许从管理器本机调用 |
| PUT | `/api/v1/users/{id}/password` | 修改密码并撤销该用户既有会话 |
| DELETE | `/api/v1/users/{id}` | 删除其他非末位用户 |
| GET | `/api/v1/snapshot` | 当前主机、Docker 和 GPU 快照 |
| GET | `/api/v1/history` | 资源历史，使用 `window` 查询参数 |
| GET | `/api/v1/host-services` | 主机服务摘要 |

### 已登记服务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/registered-services` | 已登记服务列表 |
| GET | `/api/v1/services` | 已登记服务列表的兼容别名 |
| POST | `/api/v1/registered-services` | 登记服务 |
| PUT | `/api/v1/registered-services/{id}` | 修改登记信息 |
| DELETE | `/api/v1/registered-services/{id}` | 删除登记并从场景移除 |
| POST | `/api/v1/registered-services/{id}/status` | 单次调用脚本 `status` |
| POST | `/api/v1/registered-services/{id}/actions` | 提交 `start`、`stop` 或 `restart` |
| POST | `/api/v1/registered-services/actions/stop-all` | 创建停止全部服务的操作 |

登记请求字段还包括 `wsl_portproxy_enabled`、`wsl_distro`、`wsl_listen_address`、`wsl_listen_port` 和 `wsl_connect_port`。`health_url` 只允许本机 loopback HTTP/HTTPS；`health_expect` 可留空，非空时要求响应正文包含该文本。WSL 映射必须显式启用，管理器不会根据普通服务端口猜测并扩大局域网暴露范围；未知映射、目标冲突、同步失败或 IP Helper 未实际监听都会阻止对应服务启动并通过健康接口明确报告。

服务列表读取和页面刷新不会调用脚本 `status`。管理器以固定 5 秒周期在进程内直接检查健康地址，单次超时 1 秒、并发上限 2；连续两次失败才改变稳定状态。后台检查不启动 PowerShell、WSL、Docker CLI 或其他子进程。`status` 接口是用户主动触发的深度检查；没有默认场景的管理器启动会串行调用每个服务的 `status`，并在整轮结束后重试第一轮的 `unknown`，失败的生命周期动作也会额外调用一次，以真实状态校准期望状态。

健康端点不可达时会结合期望状态判断：期望停止的服务保持 `stopped`，期望运行的服务标为 `unhealthy`；期望状态未知但最近一次明确观察为 `unhealthy` 时，因不可达或超时返回 `unknown` 的轻量探测保留该明确结果。动作后的即时验证使用该动作刚写入的新期望状态，不读取动作前的缓存值；因此停止脚本成功后端点超时会正确记为 `stopped`。这样可避免 Windows 端口转发仍接受连接但后端已停止时产生超时误报。

### 场景与操作记录

只有至少加入一个场景的登记服务属于场景管理范围。未加入任何场景的登记服务仍保留健康监控和单服务手动动作，但 `_run_scene_operation` 不会停止它们，场景 `active` / `inactive` / `partial` 状态也忽略它们。目标场景之外、但属于其他任一场景的服务仍按排他规则停止。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/scenes` | 场景列表及计算状态 |
| POST | `/api/v1/scenes` | 新建场景 |
| POST | `/api/v1/scenes/reorder` | 保存场景卡片顺序 |
| PUT | `/api/v1/scenes/{id}` | 修改场景和服务顺序 |
| DELETE | `/api/v1/scenes/{id}` | 删除场景，不操作服务 |
| PUT | `/api/v1/scenes/{id}/default` | 将场景设为项目唯一默认场景 |
| DELETE | `/api/v1/scenes/{id}/default` | 取消该场景的默认设置 |
| POST | `/api/v1/scenes/{id}/activate` | 创建场景切换操作 |
| GET | `/api/v1/operations` | 操作列表 |
| GET | `/api/v1/operations/{id}` | 操作步骤和结果 |
| POST | `/api/v1/operations/{id}/cancel` | 取消尚未执行的后续步骤 |
| GET | `/api/v1/audit` | 审计事件 |

### 视频任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/video-jobs` | 仅限本机、无需认证地提交持久化视频任务；相同 `idempotency_key` 幂等返回 |
| POST | `/api/v1/video-job-batches` | 仅限本机原子提交有序 `workflows` 批次；逐段释放资源，批尾统一回调 |
| GET | `/api/v1/video-jobs` | 已登录用户查看任务列表，`limit` 默认 100、范围 `1..500` |
| GET | `/api/v1/video-jobs/{id}` | 已登录用户查看单个任务、阶段、`prompt_id`、进度和输出 |
| POST | `/api/v1/video-jobs/{id}/cancel` | 已登录用户请求取消排队或运行中的任务 |

### 自动任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/automatic-tasks` | 已登录用户分页查看任务列表和统计；`limit` 范围 `1..500`，`offset` 从 0 开始，响应含 `has_more` |
| POST | `/api/v1/automatic-tasks` | 已登录管理员使用 CSRF 新增任务，只提交 `content` |
| POST | `/api/v1/automatic-tasks/reorder` | 已登录管理员使用 CSRF 保存执行顺序；`previous_task_ids` 是页面读取的原顺序，`task_ids` 是新顺序，两者必须完整且不重复地包含全部 `pending` 任务；原顺序已变化时返回冲突 |
| PUT | `/api/v1/automatic-tasks/{id}` | 已登录管理员使用 CSRF 编辑非运行任务，并重置为 `pending` |
| DELETE | `/api/v1/automatic-tasks/{id}` | 已登录管理员使用 CSRF 删除非运行任务 |
| POST | `/api/v1/automatic-tasks/{id}/reset` | 已登录管理员使用 CSRF 把任务人工重新排队，并使旧领取令牌失效 |
| POST | `/api/v1/automatic-tasks/claim` | 仅限本机 OpenCode，使用当前 `session_id` 按已保存队列顺序原子领取首个 `pending` 任务 |
| POST | `/api/v1/automatic-tasks/{id}/heartbeat` | 仅限领取任务的本机 OpenCode 会话，使用 `execution_token` 续期 |
| POST | `/api/v1/automatic-tasks/{id}/finish` | 仅限领取任务的本机 OpenCode 会话，使用 `execution_token` 幂等写入终态；失败时必须提供 `summary` |

页面只要求输入任务内容，标题由首个有效句子自动压缩生成，并通过不随任务状态变化的创建顺序分页完整加载后在页面按执行顺序展示。未执行任务可通过上移、下移按钮调整顺序；保存时服务端原子核对页面读取的旧顺序和全部 `pending` 任务，发现并发变化时明确拒绝，OpenCode 严格按成功保存的顺序领取。执行中的任务人工重新排队时追加到当前队尾。`axis-automatic-tasks` Skill 调用插件的领取、续期和完成工具；插件领取后每分钟后台续期 30 分钟租约，长时间工具调用期间也持续续期。过期领取在下一次领取时自动回收，旧令牌不能回写。完成一项并回写状态后才领取下一项，单项失败会记录原因并继续。运行中的任务不能从页面编辑或删除，但可人工重新排队；另一 OpenCode 会话不能并发领取。运行 `integrations/opencode/Install-AxisAutomaticTasks.ps1` 可把插件和 Skill 安装到当前用户配置目录，重启 OpenCode 后即可通过“启动自动任务”触发。

### HTTP 文件服务

管理器进程启动时同时在 `file_service_port`（默认 `18765`）启动独立 HTTP 文件服务，绑定地址与 `host` 相同，根目录由 `file_service_root`（默认 `D:/共享/`）指定。独立服务提供 `GET /health`、`GET /api/v1/files?path=&sort_by=&sort_order=` 和 `GET /api/v1/files/content?path=&download=`；它不使用 AXIS 登录态，供可信局域网直接下载或播放，禁止暴露到公网。主管理端口提供以下登录保护的同源接口供页面使用：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/file-service` | 返回端口、根目录和根目录可用状态 |
| GET | `/api/v1/file-service/files` | 使用可选 `path` 列出根目录或子目录；`sort_by` 支持 `modified`、`name`、`size`，`sort_order` 支持 `asc`、`desc`，默认按修改时间倒序且目录优先 |
| GET | `/api/v1/file-service/content` | 使用必填 `path` 流式读取文件；`download=true` 返回附件下载 |
| POST | `/api/v1/file-service/upload` | 已登录管理员使用 CSRF 把原始请求体上传到可选 `path` 目录；`name` 为必填文件名，同名文件返回冲突且不覆盖 |
| POST | `/api/v1/file-service/rename` | 已登录管理员使用 CSRF 将 `path` 指定的文件或目录更名为同目录下的 `new_name`；拒绝路径分隔符、越界和同名覆盖 |
| DELETE | `/api/v1/file-service/entry` | 已登录管理员使用 CSRF 将查询参数 `path` 指定的文件或整个目录移入 Windows 回收站；禁止删除根目录、越界项目和上传临时文件 |

路径参数统一使用相对根目录的 `/` 分隔路径并支持 UTF-8 中文名称。服务在解析符号链接和规范化路径后验证目标仍位于根目录内，拒绝任何最终指向根目录之外的路径或链接。文件响应使用磁盘流式发送并支持 HTTP Range 请求，满足大文件下载和音视频拖动播放。上传仅在登录保护的主管理端口开放，绕过通用 64 KiB JSON 请求体缓冲后按 1 MiB 批次流式写入同目录临时文件，并以硬链接原子发布来保证同名不覆盖；独立 18765 服务保持只读。页面默认使用缩略图视图，并可切换为列表；缩略图使用 `9:16` 竖图图框并直接预览图片和视频，其中视频使用 `object-fit: contain` 完整显示原始画面，其余类型显示文件类型图标；视口不超过 820px 时固定为两列。缩略图和文件名是唯一的项目操作入口：目录点击进入、普通文件点击下载、音视频点击播放，项目行不再显示重复的打开、下载或播放按钮；视频会打开播放器并请求全屏。

提交体只引用 OpenCode 已准备好的资源，不在 AXIS 内创建提示词、参考图、音频或工作流：

```json
{
  "idempotency_key": "project-session-video-001",
  "session_id": "ses_xxx",
  "workflow_path": "D:\\AIWork\\job\\h3-api-workflow.json",
  "workflow_file_sha256": "<64 位小写 SHA-256>",
  "output_path": "D:\\AIWork\\job\\final.mp4",
  "scene_name": "视频生成",
  "callback_url": "http://127.0.0.1:61714",
  "callback_directory": "D:\\AIWork\\job"
}
```

`workflow_path` 必须是现有 JSON 绝对路径，内容是 ComfyUI API workflow 的 `prompt` 对象；可选的 `workflow_file_sha256` 是原文件的 64 位小写 SHA-256，AXIS 在读取并固化同一份字节时校验。提交时内容会固化进任务记录，之后修改原文件不会改变已排队任务。`output_path` 可省略；指定时必须是绝对路径且不会覆盖现有文件，省略时写入 `video_output_directory/<job_id>/`。`scene_name` 可指定现有生成场景名称，省略时使用场景设置中唯一的默认生成场景。视频以分块方式写入同目录临时文件，再原子改名。`callback_url` 只允许无路径、查询、片段或凭据的 loopback 地址，`callback_directory` 可传递原 OpenCode 工作目录。提交和回调均不携带认证信息；AXIS 在收尾后调用 OpenCode 插件的本机回调桥，瞬时失败最多退避重试三次。查询和取消仍使用 AXIS 管理界面的登录态及 CSRF。

多段视频使用 `/api/v1/video-job-batches`，其中 `workflows` 为 1..100 个按顺序排列的 `{workflow_path, workflow_file_sha256, output_path}` 对象。AXIS 持久化 `batch_id`、`batch_index`、`batch_size`，每段完成后调用 ComfyUI `/free`，中间段不回切场景和回调，批尾或首次失败时只回调一次。任务列表响应同时返回权威 `queue_summary`，其中 `queued_segments` 只统计状态为 `queued` 的待处理段。

每个成功视频段还会保留原始输出，并将副本原子发布到 `file_service_root/video-jobs/<任务 ID>/<文件名>`。任务列表为已发布副本返回 `shared_output_path`，页面把原完整输出路径显示为可点击的同源文件服务链接；OpenCode 完成回调返回 `http://127.0.0.1:<file_service_port>/api/v1/files/content?path=...` 直接链接。复制失败或同一任务目标中存在不同内容时，任务明确失败而不覆盖文件；AXIS 重启后会从已收集的原始输出继续幂等发布。

`integrations/opencode/plugins/axis-video.ts` 注册单任务 `axis_video_submit` 和多段 `axis_video_submit_batch` 工具并自动读取当前 `sessionID` 与工作目录，同时在随机 loopback 端口创建无认证回调桥；收到 AXIS 汇总结果后通过 OpenCode 内部客户端继续原会话。取消回调把取消定义为生成终态，明确禁止 OpenCode 自动重新生成、重新提交或继续批次后续片段；只有用户在取消之后提出新的明确生成要求时才允许创建新任务。回调开始前已接受的取消覆盖成功或失败结果；任务进入 `callback_pending` 后不再接受取消，前端同时隐藏取消按钮，形成明确的取消截止点。项目在 `integrations/opencode/skills/` 中同时保留 `axis-video` 调度 Skill 和 `h3-ref2v-video-pipeline` 工作流 Skill，后者包含去敏的 4/8 步基线、API 图构建、直接提交和成片收尾脚本。运行 `integrations/opencode/Install-AxisVideo.ps1` 可把插件及两项 Skill 安装到当前用户的 OpenCode 配置目录，重启 OpenCode 后输入“使用场景切换技能生成视频”即可触发。

当生成请求只给出素材名称而没有下载链接或绝对路径时，两项 Skill 统一调用 `h3-ref2v-video-pipeline/scripts/resolve_shared_input.py`，仅从 `file_service_root/输入/` 解析视频或图片。可传 `--root` 使用非默认根目录，名称可为完整文件名或唯一文件 stem；目录片段、非媒体文件、缺失和多重匹配都会明确失败。`build_api.py --source` 对仅含文件名的值自动执行相同的视频解析。

插件按任务或批次分别记录当前会话的未就绪状态，并通过 `GET /session/{session_id}/handoff_ready/{job_or_batch_id}` 向 AXIS 暴露空闲握手：OpenCode 会话为 `busy`/`retry` 时返回 `425`，触发 `session.idle` 或状态变为 `idle` 后返回 `204`。完成回调或用户普通消息触发的新响应都会重新阻止同一会话的其他待执行任务，避免并发任务绕过握手。AXIS 只有在收到 `204` 后才检查 NInfer 空闲并切换视频场景；握手超时则保持 NInfer 运行并以 `opencode_handoff_timeout` 失败。对没有该路由而返回 `404`/`405` 的旧插件保持兼容。

调度器先获取 RTX 4090 独占租约并持久化当前完整激活的原场景，阻止等待期间出现新的人工场景切换，再同时检查 NInfer `/slots` 和 `/metrics`；只有所有 slot 空闲且 `requests_processing=0`、`requests_deferred=0` 才切换到请求指定的生成场景，未指定时切换到默认生成场景。随后验证 ComfyUI `/system_stats`、通过 `/prompt` 获取 `prompt_id`，使用任务专属 `client_id` 连接 `/ws` 接收按 `prompt_id` 过滤的当前节点和真实 `value/max` 采样进度，同时继续轮询 `/queue` 与 `/history/{prompt_id}`作为完成、失败及断线兜底，最后通过 `/view` 收集视频。成功、失败和取消都恢复持久化的原场景，最后异步回调原 `session_id`。AXIS 重启时恢复非终态任务；若重启发生在提交请求与 `prompt_id` 落库之间，只从 ComfyUI queue/history 的 `axis_job_id` 恢复，无法确认时明确失败并拒绝重复提交。

动作接口返回异步操作；前端通过操作详情展示进度。取消不会撤销已经完成的服务动作。设置默认场景本身不会立即切换；管理器下次启动后以 `system/startup` 提交普通场景切换操作。没有默认场景时启动过程不控制任何服务，但会逐个执行只读 `status` 并对第一轮的 `unknown` 重试一次，再把明确的 `running`/`stopped` 同步为期望状态；`unhealthy`/`unknown` 对应期望状态 `unknown`。

### 查询参数与主要响应

- `/api/v1/history` 的 `window` 使用分钟格式，默认 `15m`，范围为 `1m..1440m`。`15m` 返回原始采样，`1h` 按 15 秒分桶，`24h` 按 60 秒分桶。响应除 `samples` 外仍返回 `bucket_seconds`、`retention_minutes`、`stored_sample_count`、`stored_since` 和 `stored_until`，供客户端判断历史数据覆盖范围；当前资源监控界面不显示这些元数据。
- 资源历史的主机字段包含 CPU 负载/频率/温度、物理/提交/页面文件内存、主物理网卡收发、WSL 内存与 Swap；`gpus` 额外包含显存控制器及编码/解码负载，`disks` 按物理磁盘保存读写吞吐和平均延迟。GPU P-State、风扇、PCIe、时钟限制、进程归属和 Docker 容器资源仅属于实时快照，不写入历史。
- `/api/v1/health` 在资源历史写入失败时返回 `status: "degraded"`、`readiness.resource_history: "degraded"` 和不含底层 cause 的 `history_persistence_error`；健康监控循环异常时返回 `service_health_monitor_error` 并把 `readiness.registered_services` 标为 `degraded`。实时快照仍可用，后台任务会继续重试。
- `/api/v1/operations`、`/api/v1/audit` 和 `/api/v1/video-jobs` 的 `limit` 默认为 100，范围为 `1..500`，分别返回 `operations`、`events` 或 `jobs` 数组。
- 登录和首次设置成功返回 `authenticated`、`csrf_token`、`expires_at`；`auth/me` 返回 `username`、`expires_at`、新的 `csrf_token`。
- 服务列表返回 `{"services":[...],"status_mode":"health"}`；每个服务包含 `desired_state` 以及带 `state`、`checked_at`、`error`、`source` 的 `status` 观察结果。场景列表返回 `{"scenes":[...]}`。创建和更新接口返回创建或更新后的完整对象。
- 服务动作、停止全部和场景切换返回 `{"operation_id":"32位十六进制ID","status":"queued"}`。取消请求成功返回相同 ID 和 `cancellation_requested`；操作详情包含操作状态及步骤记录。

## 配置加载

配置优先级为环境变量、`WM_CONFIG_FILE` 指向的 JSON 文件、内置默认值。JSON 完整示例见 `config/settings.example.json`。

常用字段已经列在 README。以下环境变量用于开发、部署和高级限制：

```text
WM_CONFIG_FILE
WM_HOST
WM_PORT
WM_FILE_SERVICE_PORT
WM_FILE_SERVICE_ROOT
WM_SAMPLE_INTERVAL_SECONDS
WM_HISTORY_MINUTES
WM_COMMAND_TIMEOUT_SECONDS
WM_CRITICAL_PORTS
WM_DATABASE_PATH
WM_SESSION_TTL_SECONDS
WM_COOKIE_SECURE
WM_REQUEST_BODY_MAX_BYTES
WM_AUTH_CONCURRENCY_LIMIT
WM_SESSION_MAX_ACTIVE
WM_AUDIT_RETENTION_MAX_EVENTS
WM_AUDIT_RETENTION_DAYS
WM_LOGIN_FAILURE_MAX_ROWS
WM_OPERATION_RETENTION_MAX
WM_SCRIPT_STATUS_TIMEOUT_SECONDS
WM_SCRIPT_ACTION_TIMEOUT_SECONDS
WM_MANAGER_LOG_PATH
WM_MANAGER_LOG_LEVEL
WM_MANAGER_LOG_MAX_BYTES
WM_MANAGER_LOG_BACKUP_COUNT
WM_SETUP_DISABLED
WM_ALLOWED_PUBLIC_ORIGINS
WM_TRUSTED_PROXY_IPS
WM_COMFYUI_BASE_URL
WM_NINFER_BASE_URL
WM_NINFER_MODEL_ID
WM_VIDEO_OUTPUT_DIRECTORY
WM_VIDEO_JOB_POLL_INTERVAL_SECONDS
WM_VIDEO_JOB_IDLE_TIMEOUT_SECONDS
WM_VIDEO_JOB_SCENE_TIMEOUT_SECONDS
WM_VIDEO_JOB_GENERATION_TIMEOUT_SECONDS
```

列表与 `workstation_manager/config.py` 保持一致。布尔值使用 `true/false`，列表值按配置解析器要求传入 JSON 或逗号分隔内容。不要在仓库中提交包含本机地址、用户数据或凭据的正式配置。

`WM_ALLOWED_PUBLIC_ORIGINS` 和 `WM_TRUSTED_PROXY_IPS` 当前仅进行格式解析并保存，运行时没有 Origin 校验或可信代理处理，不能作为安全边界，也不能据此信任转发的客户端地址。需要反向代理时必须由代理和防火墙自行限制来源，并以当前直连 TCP 地址行为为准。

## 数据与并发

默认数据库是 `data/workstation-manager.db`，当前 schema 为 31，并在启动时自动迁移。schema 19 为场景增加唯一的 `is_default` 标记；schema 20 增加独立的 `detailed_description` 场景详细说明字段；schema 21 为操作记录增加权威的 `total_steps` 总步骤数；schema 22 为已登记服务增加显式 WSL `portproxy` 配置；schema 23 增加旧版场景用途、持久化 `video_jobs` 状态机和 RTX 4090 `resource_leases`；schema 24 删除视频任务的回调认证字段；schema 25 将旧版 `video_gen` 用途迁移为唯一的 `is_default_generation` 勾选项，并为视频任务保存生成场景及原场景；schema 26 增加显式视频批次 ID、段序号和总段数；schema 27 为每段任务持久化从工作流提取的视频规格，避免任务列表重复解析完整工作流；schema 28 在该规格中补充原视频标题或提示词内容说明并回填已有任务；schema 29 持久化已经成功发布到共享文件服务的相对输出路径；schema 30 增加自动任务队列、执行会话所有权和结果状态；schema 31 增加可持久化的未执行任务顺序。旧客户端更新场景时若未提交详细说明或默认生成场景字段，已有值会保持不变。同一个数据库同一时间只允许一个管理器实例使用，避免重复执行服务脚本。

服务控制面分别保存期望状态和实际观察状态。场景、总览及 GPU 服务摘要只使用实际观察状态；状态或错误变化时才写入 SQLite，连续成功检查不会每 5 秒写盘。资源监控定时采样和健康监控都不会调用服务脚本；显式深度检查、无默认场景的启动校准及失败动作校准才执行 `status`。资源采样将 CPU、内存及每张 GPU 的负载、显存、温度、功率和图形核心频率写入 SQLite，默认保留 24 小时；内存队列固定只保留最近 15 分钟。
