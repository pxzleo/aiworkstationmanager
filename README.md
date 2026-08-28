# AXIS AI 工作站管理器

[English](README.en.md) | 简体中文

AXIS 是运行在 Windows 工作站上的本地 Web 管理器，用一个简洁界面管理本机 AI 服务。它通过用户登记的脚本启动、停止和重启服务，并保留资源监控、场景切换、用户管理和操作记录。

## 主要功能

- 登记 `.ps1`、`.cmd` 或 `.bat` 服务管理脚本
- 启动、停止、重启、检查单个服务状态，以及一键停止全部服务
- 创建并拖动排序工作场景，一键切换一组服务
- 按独立分区展示 CPU、内存及每张 NVIDIA GPU，提供统一刻度、当前/平均/峰值/最低值和关键硬件指标
- 记录服务启停和场景切换的时间、步骤及结果
- 支持中文、英文和浏览器语言自动检测
- 提供矩阵绿、极光蓝和曜石金三种显示风格
- 支持多用户管理

## 安装与启动

需要 Windows 和 Python 3.11 或更高版本。

```powershell
python -m pip install -r requirements.txt
python -m workstation_manager
```

浏览器打开：

```text
http://127.0.0.1:19100
```

首次访问时创建管理员，密码至少 4 个字符。也可以双击 `Start-Manager.cmd`，或者运行 `Start-Manager.ps1`。

## 登记服务

打开“已登记服务”，填写服务名称、说明和管理脚本绝对路径。GPU 标签、端口和 UI 地址都是可选项，仅用于界面展示和打开服务页面。

脚本必须支持以下四个动作：

```powershell
D:\AIWork\example\manage.ps1 start
D:\AIWork\example\manage.ps1 stop
D:\AIWork\example\manage.ps1 restart
D:\AIWork\example\manage.ps1 status
```

管理器不会持续轮询脚本。启动、停止和重启成功后会保存服务状态；只有用户点击“检查状态”时才调用一次 `status`。

完整脚本规范与示例见 [脚本要求.md](脚本要求.md)。

## 使用场景

在“工作场景”中创建场景并选择需要启动的已登记服务。切换场景时，管理器会先停止当前保存状态为“运行中”且不属于目标场景的服务；只有全部必要停止都成功后，才会按场景顺序启动目标服务。

切换窗口会显示每一步的进度，可以终止尚未执行的后续步骤。已经完成的启停操作不会自动回滚。

## 常用配置

需要修改配置时，先复制示例文件：

```powershell
Copy-Item .\config\settings.example.json .\config\settings.json
.\Start-Manager.ps1 -ConfigFile .\config\settings.json
```

普通使用通常只需要以下字段：

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 监听地址；局域网访问可在完成首次设置后改为 `0.0.0.0` |
| `port` | `19100` | 管理器端口 |
| `database_path` | `data/workstation-manager.db` | 用户、服务、场景和操作记录数据库 |
| `sample_interval_seconds` | `5` | 资源监控采样间隔，不会调用服务脚本 |
| `history_minutes` | `1440` | 资源历史的 SQLite 保留时长（分钟） |
| `script_status_timeout_seconds` | `3` | 手动检查单个服务状态的超时 |
| `script_action_timeout_seconds` | `600` | 启停服务的超时 |

资源监控默认每 5 秒写入一次 SQLite，保留最近 24 小时；页面可切换 `15m`/`1h`/`24h`，其中长时间范围由服务端聚合后返回。内存中只保留最近 15 分钟，不会因 24 小时历史持续占用大量内存。

局域网模式没有 HTTPS，账号密码会以未加密 HTTP 传输，只适合可信局域网，不要直接暴露到公网。

## 随系统启动

安装登录后启动任务：

```powershell
.\Install-ManagerTask.ps1
```

安装系统启动任务并指定配置：

```powershell
.\Install-ManagerTask.ps1 -Trigger Startup -ConfigFile .\config\settings.json
```

卸载任务使用 `.\Uninstall-ManagerTask.ps1`。计划任务只启动管理器，不会自动启动任何已登记服务。

## 更多文档

- [脚本接口要求](脚本要求.md)
- [开发文档、完整 API 与高级配置](DEVELOPMENT.md)
- [English README](README.en.md)
