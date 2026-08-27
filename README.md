# AXIS AI 工作站管理器

当前包含已接入真实数据的响应式 Web 界面和 Windows 本机 FastAPI 控制面。界面读取主机、NVIDIA GPU、历史曲线、Docker 容器、关键端口、固定白名单 WebUI、受限日志、脚本发现、环境/场景预检、异步操作与审计事件；不生成随机模拟指标。生命周期控制和外部集成默认关闭，仓库不会创建任何启用配置，也不会从示例或发现脚本自动获得访问/执行权限。

## 安装与启动

Python 环境需安装 `requirements.txt` 中的依赖。首次安装：

```powershell
python -m pip install -r requirements.txt
```

运行测试时安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

直接运行非交互启动脚本：

```powershell
.\Start-Manager.ps1
```

也可从任意当前目录运行 `Start-Manager.cmd`，或显式选择设置文件：

```powershell
.\Start-Manager.ps1 -ConfigFile .\config\settings.json
```

`Install-ManagerTask.ps1` 与 `Uninstall-ManagerTask.ps1` 仅供用户手动运行。默认安装当前用户“登录时”任务；`-Trigger Startup` 使用当前用户 S4U、不会保存密码，但可能无法访问仅交互会话可见的网络资源。脚本只处理精确任务名 `AXIS-AI-Workstation-Manager`，工作目录固定为本项目，日志写入 `logs/manager.log`。安装、卸载脚本不会启动、停止或重启任何 AI 服务。

默认仅监听 `127.0.0.1:19100`，打开：

```text
http://127.0.0.1:19100
```

API：

- 公开：`/api/v1/health`、`/api/v1/auth/status`、`/api/v1/auth/setup`、`/api/v1/auth/login`
- 会话：`/api/v1/auth/me`、`/api/v1/auth/logout`
- 只读资源：`/api/v1/snapshot`、`/api/v1/history?window=15m`、`/api/v1/services`
- 发现与审计：`/api/v1/discovery/scripts`、`/api/v1/discovery/scripts/scan`、`/api/v1/audit?limit=100`
- 控制只读视图：`/api/v1/environments`、`/api/v1/scenes`、`/api/v1/operations?limit=50`
- WebUI：`/api/v1/webuis`、同源 `/proxy/webui/{id}/...`
- 日志：`/api/v1/log-sources`、`/api/v1/log-sources/{id}/entries?lines=200&since=1h`
- 预检：`POST /api/v1/environments/{id}/preflight`、`POST /api/v1/scenes/{id}/preflight`
- 经确认的异步动作：`POST /api/v1/environments/{id}/actions`、`POST /api/v1/scenes/{id}/activate`

`GET /api/v1/discovery/scripts` 同时返回 `latest_scan`。从未扫描时为 `null`；之后即使目录不存在、目录为空或单条解析失败，也会保存本次扫描 ID、时间、目录、条目数和结构化错误。

首次设置只能从 loopback 发起，密码至少 12 个字符。未完成初始化时配置非 loopback 监听会直接拒绝启动。同一来源 5 分钟内连续 5 次登录失败后会限制后续尝试。设置或登录成功后响应会返回 `csrf_token`；刷新页面后，认证态 `GET /api/v1/auth/me` 会为当前会话追加并返回一个新的 `csrf_token`。同一会话最近 8 个令牌可供多个标签页并行使用，超出后淘汰最旧值。后续状态修改请求需将任一仍有效的值放入 `X-CSRF-Token` 请求头。数据库始终只保存 CSRF 哈希，前端只在内存中保存明文。会话 Cookie 为 `HttpOnly` 且 `SameSite=Strict`。

## 配置

可使用环境变量 `WM_HOST`、`WM_PORT`、`WM_SAMPLE_INTERVAL_SECONDS`、`WM_HISTORY_MINUTES`、`WM_COMMAND_TIMEOUT_SECONDS`、`WM_CRITICAL_PORTS`、`WM_DATABASE_PATH`、`WM_DISCOVERY_SCRIPTS_PATH`、`WM_SESSION_TTL_SECONDS`、`WM_COOKIE_SECURE`、`WM_SCAN_SCRIPTS_ON_STARTUP`。安全与资源上限可用 `WM_REQUEST_BODY_MAX_BYTES`、`WM_AUTH_CONCURRENCY_LIMIT`、`WM_SESSION_MAX_ACTIVE`、`WM_AUDIT_RETENTION_MAX_EVENTS`、`WM_AUDIT_RETENTION_DAYS`、`WM_LOGIN_FAILURE_MAX_ROWS`、`WM_DISCOVERY_MAX_FILE_BYTES`、`WM_DISCOVERY_MAX_ENTRIES`、`WM_DISCOVERY_MAX_SHORTCUTS`、`WM_DISCOVERY_TOTAL_TIMEOUT_SECONDS`、`WM_OPERATION_RETENTION_MAX` 调整。`WM_CONTROL_CONFIG_PATH` 与 `WM_INTEGRATIONS_CONFIG_PATH` 分别指定控制和外部集成配置；两份正式文件默认都不存在。管理器日志可用 `WM_MANAGER_LOG_PATH`、`WM_MANAGER_LOG_LEVEL`、`WM_MANAGER_LOG_MAX_BYTES`、`WM_MANAGER_LOG_BACKUP_COUNT` 配置，默认 5 MiB、5 个备份。完整字段示例见 `config/settings.example.json`。默认数据库为 `data/workstation-manager.db`，脚本目录按当前用户解析为 `Desktop\本地模型启动`，会话有效期 12 小时，应用启动时做一次不阻断启动的只读扫描。HTTPS 反代还需设置 `WM_COOKIE_SECURE=true`、`WM_ALLOWED_PUBLIC_ORIGINS=https://manager.example.lan` 与 `WM_TRUSTED_PROXY_IPS=127.0.0.1`；后两项默认空且不允许通配符/DNS 代理地址。

## WebUI 与日志集成

复制 `config/integrations.example.json` 为 `config/integrations.json` 后，逐项现场核对，再将需要启用的条目设为 `configured:true`。正式文件不存在时，示例永远只以 `example_preview` 显示并被服务端强制关闭，不会做健康探测、代理或日志命令。目标 URL 只接受 `http/https`、字面 loopback IP 和显式固定端口；禁止 DNS 别名、用户信息、query、fragment、基础路径和未知 WebUI ID。

NInfer 示例分别声明独立的模型后端探测：4090 为 `127.0.0.1:8080/health`，3090 为 `127.0.0.1:18030/health`，仍全部 `configured:false`。页面分别显示“监控界面”和“模型后端”状态；UI 在线而模型后端离线时仍可打开隔离只读预览，但会醒目标警，绝不会把后端误报为在线。可选 `backend_probe.json_equals` 对 JSON 点分字段做精确标量匹配。ComfyUI 正式控制映射为开发 Krea2 生图/H3 视频 `8189 → RTX 3090`，视频主服务 `8000 → RTX 4090`，视频音频辅助 `8001 → RTX 3090`；三个实例使用独立职责，其中 8189 与 8001 使用不同用户目录和数据库。LM Studio Monitor 才是已从启动脚本核实的 `8765`。

同源代理是隔离只读预览，只允许 GET/HEAD，限制响应大小、并发和超时，关闭环境代理与自动跳转，过滤 Cookie、Authorization、CSRF、hop-by-hop 及其他非白名单请求头。HTML 强制浏览器 `sandbox allow-scripts`，以 `default-src 'none'` 为基线，仅对本次管理器 origin 开放必要的 script/style/img/font/connect 与受控 `base-uri`；禁止表单、嵌套、`allow-same-origin` 和 `allow-top-navigation`，不能读取管理器 API、会话或 CSRF，也不能任意连接外部 HTTPS origin。首次 HTML 请求必须已登录；静态资源使用与当前会话和固定 WebUI ID 绑定、只存哈希、120 秒过期且数量有界的只读 capability 路径，登出即撤销，审计不记录 capability。路径拒绝绝对 URL、反斜杠、控制字符和任意层编码后的 `..`。外部 `Location` 跳转会失败，只有同一上游 origin 可改写回隔离资源路径。相对资源和常见根绝对 `src/href/action` 会最小改写。MVP 不支持 POST、上游 Cookie、WebSocket、SSE 或完整 SPA 交互；它不是完整交互式 WebUI 替代品。

外部日志来源只支持固定容器名的 `docker_logs` 和固定 distro/scope/unit 的 `wsl_journal`。命令参数数组固定、`shell=False`、隐藏窗口，且有超时、行数、时间窗口、输出大小、ANSI 清理和敏感信息脱敏；不存在任意命令、文件路径或 grep 参数。管理器自身日志始终是固定来源，读取同样限幅/脱敏；其路径只允许本地磁盘，拒绝可能长期阻塞且无法安全取消的 UNC/设备路径。日志读取结果或失败会写审计。示例中的 NInfer 来源均未启用，也不会执行真实日志命令。

## 启用生命周期控制

1. 复制 `config/control.example.json` 为 `config/control.json`。示例中的已知环境仍全部为 `configured:false`，因为仓库没有足够证据确认本机的 WSL 发行版、Compose 绝对目录、Compose service/project、systemd scope/unit，以及 H3、3090 视频辅助、ASR/TTS 的准确入口。文件缺失时示例只以 `source=example_preview` 展示，程序强制 `control_enabled=false`；即使示例被误改成 `true` 也绝不会获得执行权限。
2. 逐项现场核对后，只为确认无误的环境填写一种强类型 `adapter`：`wsl_systemd`、`wsl_systemd_root`、`wsl_docker_compose` 或 `windows_comfyui_process`；同时填写严格 `health_checks` 数组与最小 `allowed_actions`，再把该环境改为 `configured:true`。root systemd 固定使用 `-u root`；Windows ComfyUI 只接受固定 Python、`main.py`、目录、端口和 GPU 映射，不执行 Desktop GUI、脚本或任意参数。
3. 先保持全局 `control_enabled:false` 检查环境与场景预检。确认完整计划、冲突白名单和健康状态均正确后，才可显式改为 `true`。

`wsl_systemd` 仅接受 `distro`、`scope`（`user`/`system`）、严格以 `.service` 结尾的 `service` 与最长 660 秒的有限超时；本机 ASR/TTS 固定为 630 秒，并在 `Type=simple` 报 active 后额外用 600 秒有界窗口轮询严格健康，覆盖模型冷启动。管理器关闭会等待这一已可能改变服务状态的轮询完成，再走统一协调/回滚/恢复锁，不能把它伪装成取消。`wsl_docker_compose` 仅接受 `distro`、规范化绝对 WSL `project_dir`、严格 `service`、可选固定 `project` 与有限超时。未知字段、任意命令、相对路径、Shell 元字符和未登记动作都会被拒绝。

`health_checks` 是封闭白名单：除 adapter、loopback TCP/HTTP、固定 `nvidia-smi` 进程核对外，WSL user systemd GPU 服务可使用 `wsl_systemd_gpu_binding` 交叉核对 host GPU index/UUID 和 unit 内唯一的 `CUDA_VISIBLE_DEVICES`；WSL Docker Compose 可使用 `wsl_docker_compose_gpu_binding` 同时核对 host index/UUID、Compose DeviceRequests、容器环境及容器内 CUDA Driver 实际枚举结果。不能用 WSL 容器里的 `nvidia-smi` 可见卡数代替 CUDA 枚举，因为 DXG 下该命令仍可能列出宿主全部 GPU。GPU AI 环境必须同时提供 adapter 状态、非空 HTTP JSON 身份匹配及适配器对应的 GPU 绑定检查；否则环境和场景均被阻断。

`preflight_checks` 同样是封闭的强类型数组：支持 JSON/Prometheus drain、Comfy 两数组 queue drain、GPU UUID 显存余量、WSL/Windows 路径与磁盘余量、依赖、loopback/WSL 内部端口和静态 `h3_video_profile`。使用 Windows portproxy 的 WSL 服务必须用与当前 adapter 发行版、环境 ID 和健康端口一致的 `wsl_port_available`，不能把 portproxy 的 Windows 监听当成冲突。SenseVoice/IndexTTS 的 stop/restart 先等待活动请求清零，再由 Uvicorn 优雅关闭停止接收新连接并在 300 秒窗口内完成竞态在途请求；未新增任何 LAN 可调用的生命周期控制路由。

本机正式 `config/control.json` 中 NInfer 4090、NInfer 4090 UI、ComfyUI Krea2 生图/H3 视频 8189、SenseVoiceSmall 18090 与 IndexTTS 1.5 vLLM 6006 已登记为可控服务。8189 使用固定 Python/main.py、`--cuda-device 1`、RTX 3090 UUID 和独立 `user-image-3090` 数据库，并预检 Krea2/H3 全套模型文件及 H3 8-step profile；NInfer Compose 模板、CUDA 可见性探针源码和 UI unit 分别位于 `config/wsl-compose/`、`config/wsl-tools/`、`config/wsl-units/`。NInfer/ComfyUI/ASR/TTS 均由单环境动作或开发/agent场景切换按需启动，不自动占用模型显存。其他 `configured:false` 环境仍只读显示真实状态并禁止写动作。

部署 CUDA 探针时，在 WSL 中使用项目已安装的 `g++` 将 `config/wsl-tools/cuda-visible-probe.cpp` 链接 `/usr/lib/wsl/lib/libcuda.so.1`，输出到 NInfer 项目的 `manager-tools/cuda-visible-probe` 并设为 0755；Compose 将其只读挂载为 `/usr/local/bin/cuda-visible-probe`。正式配置要求 stopped 冷启动前 4090 至少空闲 47,000 MiB。restart 固定执行“drain → stop → 显存/模型/端口检查 → start”，不会拿运行中仅剩的显存余量冒充冷启动预算。

总览顶部、工作场景页标题区和两个场景卡片都提供醒目的“一键切换”入口。点击后先运行服务状态、队列、显存、路径和依赖预检；存在 blocker 时只返回原因，全部通过后才要求精确确认并提交切换，不会因为场景暂不可执行而把按钮误标为“尚未配置”。

安全警告：启用控制等同于授予管理器调用 WSL systemd/Docker Compose 生命周期接口的高权限。管理器始终使用固定参数数组、`shell=False`、隐藏窗口、超时、输出上限和脱敏，但配置管理员仍必须采用最小权限。不要把交互式 `.cmd`/`.ps1`、Docker Socket、任意 URL、任意路径或通用命令模板放进控制配置。脚本扫描结果永远不会自动转成可执行动作。

环境 `start`/`restart` 会在提交和后台执行前重复执行同一套静态配置完整性校验，直接调用动作 API 也不能绕过。GPU AI 缺少 HTTP JSON 模型精确匹配、GPU UUID 进程检查、显存预算、模型路径/磁盘或端口检查时不会启动。普通非 AI 环境的 `stop` 只要求固定适配器和显式权限；可控 AI 的 stop/restart 以及失败后的 rollback-stop 都必须先轮询匹配 drain 到 0，任一失败绝不调用 adapter，并进入持久恢复锁供人工处理。

NInfer Compose 使用 `restart: "no"`，避免 Docker/WSL 恢复时绕过场景冲突和冷启动检查；UI user unit 单独随 user manager 启动。NInfer stop grace 为 600 秒。当前 NInfer 没有“停止接收新请求后排空”的 quiesce 接口，因此读取 drain=0 到发出 stop 之间仍有极短竞态；系统不把它宣称为完全无损排空。8080 按现有工作站需求继续发布到 `0.0.0.0` 且后端未启用 API key，只允许受信任私有局域网/主机防火墙边界，禁止公网暴露。

`windows_path_disk.timeout_seconds` 通过可终止的隔离子进程强制执行；超时返回 unknown 并阻断操作。Windows 路径逐级拒绝 symlink/junction/reparse，且拒绝 ADS 与 CON/PRN/AUX/NUL/COM1-9/LPT1-9 等保留设备名（包括带扩展名形式）。

动作异常或超时后管理器会重新核对实际状态；仅当目标状态与必需健康检查均满足时才协调为成功。无法确认或安全回滚时会写入持久恢复锁并阻断全部后续控制，页面显示“需要人工恢复”。场景锁会保存每个受影响环境的明确 `running`/`stopped` 恢复目标；恢复到 running 还必须重过完整 endpoint/model/GPU 健康检查。管理员逐项人工恢复后执行严格恢复预检：单环境输入 `resolve-recovery:<environment-id>`，多环境场景输入 `resolve-recovery:<operation-id>`；解除请求要求登录、CSRF、精确确认并写入审计。

如果 operation 最终状态或恢复锁因数据库故障无法可靠写入，管理器会 fail-closed：当前进程保留控制租约和文件锁、拒绝新动作，并提示必须重启。进程退出后新实例会把遗留环境操作转换为持久恢复锁；原状态无法确认时只接受人工恢复到明确的 stopped，再允许解除。状态读取为 unknown 时，具有固定适配器且显式获准的 stop 仍保持可用，以便执行安全释放。

所有环境与场景动作同时持有 SQLite 原子租约和数据库旁的进程级文件锁，避免两个管理器实例并发改动服务；崩溃释放文件锁后，新实例才会把遗留 operation/step 标记为 `interrupted` 并写恢复审计。场景计划固定为 `drain -> stop conflicts -> verify release/ports -> validate paths/disk/VRAM/profile/dependencies -> start desired -> strict verify`，并要求本次可能用到的逆向回滚动作也显式列入 `allowed_actions`。任何 unknown/failed/非预期状态或 running 健康未恢复都会进入多环境恢复锁；没有匹配恢复锁的 `recovery_required`/`rollback_failed` 终态会使控制面 fail-closed。关闭期间已进入适配器的固定命令不会被伪装成已取消：管理器等待其有限超时/完成并进行状态验证与安全收尾。

也可设置 `WM_CONFIG_FILE` 指向 JSON 文件，字段名与上述环境变量对应的小写名一致；环境变量优先于文件。

## 局域网部署

默认必须保持 `127.0.0.1`。先在本机打开 `http://127.0.0.1:19100` 完成管理员设置；后端会强制拒绝“未 setup 就绑定非 loopback”。之后才可在 `config/settings.json` 中显式设置 `host` 为 `0.0.0.0` 或本机固定 LAN IP，并通过 `-ConfigFile` 启动。程序不会自动修改 Windows 防火墙。

管理员如确需 LAN 访问，可手动在提升权限的 PowerShell 中创建仅 Private profile、固定端口、可信子网的规则（示例子网必须按现场修改）：

```powershell
New-NetFirewallRule -DisplayName "AXIS Manager 19100 Private LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 19100 -Profile Private -RemoteAddress <LAN_SUBNET>
```

不要使用 `Any` profile、任意远端地址，也不要将 19100 直接暴露到公网。首次 setup 必须直连 `http://127.0.0.1:19100`；带 `Forwarded`/`X-Forwarded-*` 或非 loopback Host 的 setup 会被拒绝。HTTP LAN 模式下 Cookie 不应设置 `Secure`，但流量和凭据未加密。使用 HTTPS 反向代理前必须已完成 setup、设置 `WM_SETUP_DISABLED=true`、`WM_COOKIE_SECURE=true`，把完整外部 origin 精确加入 `WM_ALLOWED_PUBLIC_ORIGINS`，并把反代与管理器之间的实际 TCP peer IP 加入 `WM_TRUSTED_PROXY_IPS`。管理器只用 TCP peer 判定可信代理，绝不从转发头推导客户端身份；未信 peer 携带任何 `Forwarded`/`X-Forwarded-*` 都会失败关闭。反代必须保留外部 Host、明确阻断 `/api/v1/auth/setup`，并自行做逐客户端登录限速；不能绕过管理器直接公开上游 loopback WebUI。`/proxy-asset/` 路径含短期 bearer capability，反代必须关闭该路径的访问日志，不能记录、采样或转发完整 URI。

Nginx 最小关键段（先替换证书和域名）：

```nginx
location = /api/v1/auth/setup { return 403; }
location ^~ /proxy-asset/ {
    access_log off;
    proxy_pass http://127.0.0.1:19100;
    proxy_set_header Host $http_host;
    proxy_set_header Forwarded "";
    proxy_set_header X-Forwarded-Host "";
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
}
location / {
    proxy_pass http://127.0.0.1:19100;
    proxy_set_header Host $http_host;
    proxy_set_header Forwarded "";
    proxy_set_header X-Forwarded-Host "";
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
}
```

Caddy 最小关键段：

```caddy
manager.example.lan {
    @setup path /api/v1/auth/setup
    @proxyAssets path /proxy-asset/*
    respond @setup 403
    log_skip @proxyAssets
    reverse_proxy 127.0.0.1:19100 {
        header_up Host {host}
        header_up -Forwarded
        header_up -X-Forwarded-Host
    }
}
```

若错误创建了防火墙规则，请按精确显示名手动删除：`Remove-NetFirewallRule -DisplayName "AXIS Manager 19100 Private LAN"`。管理器的卸载任务脚本不会碰防火墙或 AI 服务。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 已实现的页面

- 首次管理员设置、登录、刷新后会话恢复和退出登录
- 总览真实 CPU、内存、磁盘、Docker 与双 GPU 指标；GPU 少于两张时显示未检测到，多出的 GPU 在环境页列出
- 最近 15 分钟 CPU、内存、每张 GPU 负载与显存历史曲线；空数据、单点、离线、过期与采集器降级均有明确状态
- Docker 容器和关键监听端口只读状态、最近审计事件
- 桌面脚本真实发现结果、结构化线索/单项错误与登录态 CSRF 重新扫描
- 各读取接口在上一轮完成后再安排下一轮；登录切换会取消旧请求，快照、历史、服务、审计和发现分别显示最近成功、失败与过期状态
- 开发/视频场景、环境适配器和异步操作接入真实控制 API
- 固定白名单 WebUI 在线状态、同源 HTTP 代理与明确的未配置/离线/受限状态
- 固定 Docker/WSL journal 来源、管理器轮转日志、手动/自动刷新，以及保留的 operation/audit 视图

正式机器已使用 `config/control.json` 接入通过验收的环境；若部署包缺少该文件，场景切换和环境启停会明确禁用并展示缺失入口，不会伪装成功。`config/integrations.json` 缺失时 WebUI 代理和外部日志读取同样保持禁用。未验收的视频场景仍会明确因 H3 8000 与 3090 配套 8001 入口缺失而阻断。“打开 WebUI”只访问已在线目标，绝不隐式启停服务；脚本扫描仅读取元数据和文本，不运行脚本或快捷方式目标。

排错顺序：先看 `/api/v1/health` 的版本、schema 与 readiness 摘要，再看 `logs/manager.log`；集成配置无效时 `/api/v1/webuis` 和 `/api/v1/log-sources` 返回安全的 `config_error`，不会泄露目标或本机路径。控制页面出现恢复锁时，不要删除数据库；先把目标人工恢复到页面记录的 `running`/`stopped` 状态，再使用恢复预检和精确确认文本解除。WebUI 代理出现 502 时只表示上游不可用、响应过大或外部跳转被阻止，不会返回内部目标细节。

发布时不要直接压缩工作目录（本目录不是 Git 仓库时 `.gitignore` 不提供打包保护）。手动运行 `Build-Release.ps1 -Destination <空目录>` 生成 allowlist staging；脚本不会包含内部 `REQUIREMENTS.md`、`data/`、`logs/`、`output/`、`.playwright-cli/` 或 `config/*.json` 正式配置。
