# AXIS AI 工作站管理器

[English](README.en.md) | 简体中文

AXIS 是运行在 Windows 工作站上的本地 Web 管理器。它保留主机、CPU、内存、磁盘、Docker 和 NVIDIA GPU 的实时采样与历史曲线，并通过用户登记的管理脚本统一启停服务和切换场景。

## 安装与启动

```powershell
python -m pip install -r requirements.txt
python -m workstation_manager
```

默认地址：

```text
http://127.0.0.1:19100
```

首次访问需要创建管理员，密码至少 4 个字符。系统支持多个具有相同管理权限的用户。也可以运行 `Start-Manager.ps1` 或 `Start-Manager.cmd`。

安装登录后启动任务：

```powershell
.\Install-ManagerTask.ps1
```

安装系统启动任务并使用指定配置：

```powershell
.\Install-ManagerTask.ps1 -Trigger Startup -ConfigFile .\config\settings.json
```

卸载任务使用 `.\Uninstall-ManagerTask.ps1`。这些计划任务脚本只供用户手动执行；管理器本身不会自动创建或删除计划任务。

计划任务名为 `AXIS-AI-Workstation-Manager`，只启动管理器本身，不自动启动任何已登记服务。

## 用户管理

侧栏“用户管理”页面显示全部管理用户、创建时间、活跃会话数量和当前登录用户。所有用户具有相同管理权限。页面支持新增用户、修改任意用户密码和删除其他用户；不能删除当前登录用户或最后一个用户。修改密码会立即使该用户原有会话全部失效，当前用户修改自己的密码后需要重新登录。为保持现有部署边界，新增用户只允许从管理器本机地址执行，用户列表、修改密码和删除仍需登录及 CSRF 校验。

在包含 `tests/` 目录的源码检出中运行测试（发布包不包含测试目录）：

```powershell
python -m unittest discover -s tests -v
node --test tests/frontend_request_guard.test.js tests/frontend_contract.test.js tests/frontend_i18n.test.js tests/documentation_consistency.test.js
```

## 已登记服务

“已登记服务”是唯一的服务控制模型。添加服务时填写：

- 名称和说明
- 管理脚本绝对路径
- 可选 GPU 展示标签
- 可选服务端口
- 可选完整 HTTP/HTTPS UI 地址

管理脚本支持 `.cmd`、`.bat` 和 `.ps1`，并接受一个固定动作参数：

完整接口、错误处理和示例参见 [脚本要求.md](脚本要求.md)；英文版参见 [Script Requirements](SCRIPT_REQUIREMENTS.en.md)。

```powershell
D:\AIWork\example\manage.ps1 start
D:\AIWork\example\manage.ps1 stop
D:\AIWork\example\manage.ps1 restart
D:\AIWork\example\manage.ps1 status
```

`start`、`stop`、`restart` 成功返回退出码 0，失败返回非零退出码和错误文本。脚本应自行处理依赖、GPU 设置、端口冲突、健康判断和幂等性。

`status` 必须是快速、只读、无副作用的检查，成功时只输出以下一个值：

```text
running
stopped
unhealthy
unknown
```

服务状态永久保存到数据库，管理器启动和页面刷新不会执行 `status`。启动、停止和重启成功后直接保存目标状态；只有用户点击单个服务的“检查状态”时才调用一次 `status`。状态检查默认 3 秒超时，服务动作默认 600 秒超时。

删除服务只删除登记记录，并自动将它从全部场景移除；不会删除脚本、项目、模型或服务本身。

“停止全部服务”会按当前服务列表顺序（名称不区分大小写，再按 ID）逐个调用所有服务的 `stop`，即使某一步失败也会继续尝试其余服务，并将结果写入同一条操作记录。

## 场景

场景编辑器可以添加、编辑和删除场景，并为每个场景配置有序的已登记服务列表。

切换场景时：

1. 只对保存状态为 `running` 且未包含在目标场景中的服务调用 `stop`。
2. 状态为 `stopped`、`unhealthy` 或 `unknown` 的非目标服务不重复停止；符合条件的停止步骤任一失败则不进入启动阶段。
3. 停止全部成功后，按场景顺序调用目标服务的 `start`。
4. 单个目标服务启动失败不阻止后续目标服务。
5. 不批量调用 `status`；目标服务保存状态全为 `running` 且没有非目标服务仍为 `running` 时，场景为“已激活”。非空场景内没有任何目标服务保存为 `running` 时显示“未激活”，即使仍有非目标服务运行也保持该判定；至少一个但并非全部目标服务运行，或全部目标服务运行但仍有非目标服务运行时，显示“部分启动”。空场景在没有任何服务运行时为“已激活”，存在运行中服务时为“部分启动”。

场景切换不自动回滚。删除场景也不会停止或删除任何服务。

同一个数据库只允许一个管理器实例运行；第二个实例会明确启动失败，避免重复执行服务脚本。

## 资源监控与日志

总览保留双 GPU 卡片、CPU、内存、磁盘、Docker、GPU 负载和显存。资源监控页面保留最近 15 分钟的 CPU、内存以及每张 GPU 的负载和显存曲线。

日志中心只展示管理操作记录：

- 服务启动、停止、重启
- 场景切换及各服务步骤
- 操作时间、结果和失败摘要

添加、编辑、删除和登录审计不在日志中心显示；管理器也不读取具体服务日志。

## API

- 健康：`GET /api/v1/health`
- 认证：`GET /api/v1/auth/status`、`POST /api/v1/auth/setup`、`POST /api/v1/auth/login`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me`
- 用户：`GET/POST /api/v1/users`、`PUT /api/v1/users/{id}/password`、`DELETE /api/v1/users/{id}`
- 资源：`GET /api/v1/snapshot`、`GET /api/v1/history?window=15m`、`GET /api/v1/host-services`
- 服务：`GET/POST /api/v1/registered-services`、`PUT/DELETE /api/v1/registered-services/{id}`、`POST /api/v1/registered-services/{id}/status`、`POST /api/v1/registered-services/{id}/actions`、`POST /api/v1/registered-services/actions/stop-all`
- 场景：`GET/POST /api/v1/scenes`、`POST /api/v1/scenes/reorder`、`PUT/DELETE /api/v1/scenes/{id}`、`POST /api/v1/scenes/{id}/activate`
- 记录：`GET /api/v1/operations`、`GET /api/v1/operations/{id}`、`POST /api/v1/operations/{id}/cancel`、`GET /api/v1/audit`

`GET /api/v1/services` 是服务列表的兼容别名。`health`、`auth/status` 和 `auth/login` 不要求已有会话；首次 `auth/setup` 只允许从本机 loopback 地址执行。初始化完成后，其余读取接口需要登录，认证入口之外的管理写操作需要当前会话的 CSRF 令牌。初始化尚未完成时，本机可以读取资源采样以完成首次设置。

## 语言

界面支持中文和英文。首次访问会根据浏览器首选语言自动选择：中文浏览器使用中文，其他语言使用英文。登录页和主界面顶部都可以手动切换，选择会保存在当前浏览器并立即生效。

前端会把当前语言随 API 请求发送。API 错误码与结构保持不变，主错误信息按中文或英文返回；未指定语言的旧客户端仍使用中文。

## 配置

完整字段见 `config/settings.example.json`。主要新增字段：

- `script_status_timeout_seconds`：默认 3
- `script_action_timeout_seconds`：默认 600

默认数据库为 `data/workstation-manager.db`。当前数据库 schema 为 13，并会自动迁移；旧脚本发现、控制适配器和恢复锁表在 schema 10 中移除。

生成发布目录：

```powershell
.\Build-Release.ps1 -Destination D:\Release\axis-manager
```

发布包不会包含数据库、日志或本机服务登记数据。
