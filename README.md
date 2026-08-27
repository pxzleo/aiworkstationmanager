# AXIS AI 工作站管理器

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

首次访问需要创建管理员，密码至少 12 个字符。也可以运行 `Start-Manager.ps1` 或 `Start-Manager.cmd`。计划任务安装和卸载脚本只供用户手动执行。

运行测试：

```powershell
python -m unittest discover -s tests -v
node --test tests/frontend_request_guard.test.js tests/frontend_contract.test.js
```

## 已登记服务

“已登记服务”是唯一的服务控制模型。添加服务时填写：

- 名称和说明
- 管理脚本绝对路径
- 可选 GPU 展示标签
- 可选服务端口
- 可选完整 HTTP/HTTPS UI 地址

管理脚本支持 `.cmd`、`.bat` 和 `.ps1`，并接受一个固定动作参数：

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

后端默认每 5 秒统一调用所有空闲服务的 `status`，浏览器只读取缓存。状态检查默认 3 秒超时，服务动作默认 600 秒超时。添加、编辑、手动刷新和动作完成后会立即更新状态。

删除服务只删除登记记录，并自动将它从全部场景移除；不会删除脚本、项目、模型或服务本身。

## 场景

场景编辑器可以添加、编辑和删除场景，并为每个场景配置有序的已登记服务列表。

切换场景时：

1. 依次对所有未包含在目标场景中的服务调用 `stop`。
2. 停止阶段会尝试全部非目标服务；任一失败则不进入启动阶段。
3. 停止全部成功后，按场景顺序调用目标服务的 `start`。
4. 单个目标服务启动失败不阻止后续目标服务。
5. 最后刷新全部状态；目标服务全为 `running` 且其他服务全为 `stopped` 时场景为已激活。

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

- 认证：`/api/v1/auth/status`、`setup`、`login`、`logout`、`me`
- 资源：`/api/v1/snapshot`、`/api/v1/history?window=15m`
- 服务：`/api/v1/registered-services`、`/refresh`、`/{id}/actions`
- 场景：`/api/v1/scenes`、`/{id}/activate`
- 记录：`/api/v1/operations`、`/api/v1/audit`

除健康检查和认证入口外，API 需要登录。写操作需要当前会话的 CSRF 令牌。

## 配置

完整字段见 `config/settings.example.json`。主要新增字段：

- `service_status_interval_seconds`：默认 5
- `script_status_timeout_seconds`：默认 3
- `script_action_timeout_seconds`：默认 600

默认数据库为 `data/workstation-manager.db`。SQLite schema 会自动迁移，旧脚本发现、控制适配器和恢复锁表在 schema 10 中移除。

生成发布目录：

```powershell
.\Build-Release.ps1 -Destination D:\Release\axis-manager
```

发布包不会包含数据库、日志或本机服务登记数据。
