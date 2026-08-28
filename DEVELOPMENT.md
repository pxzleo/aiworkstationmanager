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

## API 约定

API 前缀为 `/api/v1`，请求和响应使用 JSON。错误响应保留稳定的 `error.code`，并根据 `Accept-Language` 返回中文或英文错误信息。

首次初始化的 `auth/setup` 只允许从本机 loopback 地址访问。初始化完成后，除健康、认证状态和登录外，读取接口需要登录；除 `auth/setup` 和 `auth/login` 外的写操作（包括 `auth/logout`）还需要把当前会话令牌放入 `X-CSRF-Token` 请求头。会话 Cookie 为 `HttpOnly` 和 `SameSite=Strict`。

成功响应直接返回 JSON 对象，创建接口返回 `201`，异步动作返回 `202`，删除接口返回无正文的 `204`，其他成功接口返回 `200`。错误统一为 `{"error":{"code":"...","message":"...","details":...}}`；参数校验错误返回 `422`，未登录返回 `401`，CSRF 或访问来源不符合要求返回 `403`，目标不存在返回 `404`，冲突或已有操作执行中返回 `409`。

### 请求体

认证设置、登录和新增用户使用同一请求体。`username` 去除首尾空白后必须为 3..64 个字符，`password` 为 4..1024 个字符：

```json
{"username":"admin","password":"1234"}
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
  "ui_url": "http://192.168.100.190:8189/"
}
```

`name` 最长 100 字符，`description` 最长 1000 字符，`script_path` 必须是现有 `.ps1`、`.cmd` 或 `.bat` 绝对路径，`gpu_label` 最长 100 字符，`port` 为 `1..65535`，`ui_url` 必须为空或完整 HTTP/HTTPS 地址。

登记或修改场景使用有序且不包含未知服务的 `service_ids`；重复 ID 会按首次出现去重：

```json
{"name":"开发","description":"开发服务组","service_ids":["服务ID1","服务ID2"]}
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

服务列表读取、页面刷新和管理器启动都不会调用脚本 `status`。

### 场景与操作记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/scenes` | 场景列表及计算状态 |
| POST | `/api/v1/scenes` | 新建场景 |
| POST | `/api/v1/scenes/reorder` | 保存场景卡片顺序 |
| PUT | `/api/v1/scenes/{id}` | 修改场景和服务顺序 |
| DELETE | `/api/v1/scenes/{id}` | 删除场景，不操作服务 |
| POST | `/api/v1/scenes/{id}/activate` | 创建场景切换操作 |
| GET | `/api/v1/operations` | 操作列表 |
| GET | `/api/v1/operations/{id}` | 操作步骤和结果 |
| POST | `/api/v1/operations/{id}/cancel` | 取消尚未执行的后续步骤 |
| GET | `/api/v1/audit` | 审计事件 |

动作接口返回异步操作；前端通过操作详情展示进度。取消不会撤销已经完成的服务动作。

### 查询参数与主要响应

- `/api/v1/history` 的 `window` 使用分钟格式，默认 `15m`，范围为 `1m..1440m`。`15m` 返回原始采样，`1h` 按 15 秒分桶，`24h` 按 60 秒分桶。响应除 `samples` 外仍返回 `bucket_seconds`、`retention_minutes`、`stored_sample_count`、`stored_since` 和 `stored_until`，供客户端判断历史数据覆盖范围；当前资源监控界面不显示这些元数据。
- 资源历史的主机字段包含 CPU 负载/频率/温度、物理/提交/页面文件内存、主物理网卡收发、WSL 内存与 Swap；`gpus` 额外包含显存控制器及编码/解码负载，`disks` 按物理磁盘保存读写吞吐和平均延迟。GPU P-State、风扇、PCIe、时钟限制、进程归属和 Docker 容器资源仅属于实时快照，不写入历史。
- `/api/v1/health` 在资源历史写入失败时返回 `status: "degraded"`、`readiness.resource_history: "degraded"` 和不含底层 cause 的 `history_persistence_error`。实时快照仍可用，下一采样周期自动重试，成功后清除降级状态。
- `/api/v1/operations` 和 `/api/v1/audit` 的 `limit` 默认为 100，范围为 `1..500`，分别返回 `operations` 或 `events` 数组。
- 登录和首次设置成功返回 `authenticated`、`csrf_token`、`expires_at`；`auth/me` 返回 `username`、`expires_at`、新的 `csrf_token`。
- 服务列表返回 `{"services":[...],"status_mode":"stored"}`；场景列表返回 `{"scenes":[...]}`。创建和更新接口返回创建或更新后的完整对象。
- 服务动作、停止全部和场景切换返回 `{"operation_id":"32位十六进制ID","status":"queued"}`。取消请求成功返回相同 ID 和 `cancellation_requested`；操作详情包含操作状态及步骤记录。

## 配置加载

配置优先级为环境变量、`WM_CONFIG_FILE` 指向的 JSON 文件、内置默认值。JSON 完整示例见 `config/settings.example.json`。

常用字段已经列在 README。以下环境变量用于开发、部署和高级限制：

```text
WM_CONFIG_FILE
WM_HOST
WM_PORT
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
```

列表与 `workstation_manager/config.py` 保持一致。布尔值使用 `true/false`，列表值按配置解析器要求传入 JSON 或逗号分隔内容。不要在仓库中提交包含本机地址、用户数据或凭据的正式配置。

`WM_ALLOWED_PUBLIC_ORIGINS` 和 `WM_TRUSTED_PROXY_IPS` 当前仅进行格式解析并保存，运行时没有 Origin 校验或可信代理处理，不能作为安全边界，也不能据此信任转发的客户端地址。需要反向代理时必须由代理和防火墙自行限制来源，并以当前直连 TCP 地址行为为准。

## 数据与并发

默认数据库是 `data/workstation-manager.db`，当前 schema 为 17，并在启动时自动迁移。同一个数据库同一时间只允许一个管理器实例使用，避免重复执行服务脚本。

服务的保存状态是控制面的主要状态来源。资源监控定时采样不会调用服务脚本；只有显式状态检查才执行 `status`。资源采样将 CPU、内存及每张 GPU 的负载、显存、温度、功率和图形核心频率写入 SQLite，默认保留 24 小时；内存队列固定只保留最近 15 分钟。
