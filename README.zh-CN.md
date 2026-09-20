# AXIS AI 工作站管理器

[English](README.md) | 简体中文

AXIS 将 AI 工作站的各种服务统一管理并整合进不同场景，可一键在各个场景间切换各种所需服务，并实时监控服务器状态。UI 设计适应 PC 到手机屏幕的监控和配置，可适应任意服务，让 AI 按 [scriptspec.md](scriptspec.md) 要求编写或改写现有脚本即可。它是一个界面简洁的 AI 工作站服务管理系统。

## 界面预览

**工作站总览**

![AXIS 工作站总览，展示当前场景、主机状态和双 GPU 运行情况](docs/1.png)

**工作场景**

![工作场景管理，展示场景中的服务顺序、状态和切换操作](docs/2.png)

**已登记服务**

![已登记服务列表，展示服务说明、GPU、端口、状态和管理操作](docs/3.png)

**主机资源监控**

![资源监控页面，展示 CPU 和内存历史曲线](docs/4.png)

**GPU 核心遥测相关性**

![GPU 核心负载、频率、功率和温度的全宽关联曲线](docs/5.png)

## 主要功能

- 登记 `.ps1`、`.cmd` 或 `.bat` 服务管理脚本
- 启动、停止、重启、深度检查单个服务状态，以及一键停止全部服务
- 通过低开销本机健康接口自动识别外部启停、服务异常和意外退出
- 创建并拖动排序工作场景，一键切换一组服务
- 在独立“视频任务”界面提交、取消并监控本地视频生成的完整阶段
- 通过独占 GPU 租约切换到指定或默认生成场景，完成后自动恢复原场景
- 按独立分区展示 CPU、内存及每张 NVIDIA GPU，提供统一刻度、当前/平均/峰值/最低值和关键硬件指标
- 记录服务启停和场景切换的时间、步骤及结果
- 支持中文、英文和浏览器语言自动检测
- 提供矩阵绿、极光蓝和曜石金三种显示风格
- 在系统设置中查看当前 AXIS 版本并打开 GitHub 项目
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

打开“已登记服务”，填写服务名称、说明和管理脚本绝对路径。GPU 标签、端口和 UI 地址用于界面展示；健康检查地址及可选响应匹配文本用于自动确认服务的真实状态。

脚本必须支持以下四个动作：

```powershell
D:\AIWork\example\manage.ps1 start
D:\AIWork\example\manage.ps1 stop
D:\AIWork\example\manage.ps1 restart
D:\AIWork\example\manage.ps1 status
```

管理器不会持续轮询脚本，也不会为后台状态监控启动 PowerShell、WSL 或 Docker 命令。它每 5 秒在自身进程内直接检查已登记的本机健康地址；连续两次失败才改变状态。用户点击“深度检查”时会调用一次 `status`；未配置默认场景的管理器启动以及失败的生命周期动作也会调用 `status`，用于校准实际状态并清除遗留的错误期望状态。启动校准第一轮仍返回 `unknown` 的服务会在整轮结束后再只读检查一次，以跨过 WSL 等运行环境的冷启动窗口。

完整脚本规范与示例见 [scriptspec.md](scriptspec.md)。

## 使用场景

在“工作场景”中创建场景并选择需要启动的已登记服务。只有至少加入一个场景的服务参与场景切换；未加入任何场景的登记服务仍可监控和手动管理，但不会被场景切换启停，也不影响场景状态。切换前管理器先刷新轻量健康状态，只停止场景管理范围内实际运行且不属于目标场景的服务；只有全部必要停止成功后，才会按场景顺序启动尚未运行的目标服务。

切换窗口会显示每一步的进度，可以终止尚未执行的后续步骤。已经完成的启停操作不会自动回滚。

每个项目可以选择一个“默认场景”。设置后不会立即切换；AXIS 下次启动时会自动执行该场景。取消默认只清除启动设置，不会停止当前服务；下次启动时 AXIS 只读校准全部已登记服务，不会自动启停它们。

场景不再设置 `Code Agent` 或 `Video Gen` 类型；场景编辑器只提供“默认生成场景”勾选项，且最多勾选一个。OpenCode 通过随项目提供的 `axis_video_submit` 工具向本机 `POST /api/v1/video-jobs` 直接提交 ComfyUI API 工作流，无需配置密钥、用户名、密码或认证头。调用时可指定生成场景名称；未指定时使用默认生成场景。AXIS 会持久化任务、记录当前原场景、等待 NInfer 完全空闲、独占 GPU、切换到生成场景、监控 ComfyUI `prompt_id`、保存输出，再自动恢复原场景，最后通过插件建立的本机回调桥返回原 OpenCode 会话。若 NInfer 仍有处理或排队请求，AXIS 不会切换场景。第一版只接受本机提交；显式输出路径优先，未提供时使用默认输出目录。

安装 OpenCode 集成后重启 OpenCode。安装脚本会部署项目内随附的 `axis-video` 调度 Skill、`h3-ref2v-video-pipeline` 工作流 Skill（含去敏 4/8 步基线及构建、提交与收尾脚本）以及 `axis-video` 插件：

```powershell
.\integrations\opencode\Install-AxisVideo.ps1
```

之后在 OpenCode 中输入“使用场景切换技能生成视频”，也可以在同一句中指定生成场景名称。OpenCode 会准备 ComfyUI API workflow 并调用 `axis_video_submit`；未指定名称时使用默认生成场景。插件会自动取得当前会话 ID 和工作目录，任务进度在 AXIS 的“视频任务”页面查看，完成后恢复原场景并把结果送回原会话。

自动任务使用独立的 OpenCode 集成。运行下列脚本并重启 OpenCode 后，在 AXIS“自动任务”页面新增任务，再输入“启动自动任务”；OpenCode 会按创建时间串行执行全部未执行任务。执行租约会定期续期，意外退出后可自动恢复，页面也可把执行中任务重新排队。

```powershell
.\integrations\opencode\Install-AxisAutomaticTasks.ps1
```

在私人电脑或手机登录时，可以勾选“在该电脑自动登录”保持登录 30 天。AXIS 不会在浏览器中保存密码；主动退出或修改密码仍会立即撤销会话。

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
| `file_service_port` | `18765` | 随管理器启动的 HTTP 文件服务端口 |
| `file_service_root` | `D:/共享/` | 文件服务允许浏览、下载和播放的根目录 |
| `database_path` | `data/workstation-manager.db` | 用户、服务、场景和操作记录数据库 |
| `sample_interval_seconds` | `5` | 资源监控采样间隔，不会调用服务脚本 |
| `history_minutes` | `129600` | 资源历史的 SQLite 保留时长（分钟，即 90 天） |
| `script_status_timeout_seconds` | `3` | 深度检查、启动校准及失败动作校准中单个 `status` 的超时 |
| `script_action_timeout_seconds` | `600` | 启停服务的超时 |
| `comfyui_base_url` | `http://127.0.0.1:8189` | 视频任务使用的本机 ComfyUI API |
| `ninfer_base_url` | `http://127.0.0.1:8080` | 空闲检查及恢复验证使用的本机 NInfer API |
| `video_output_directory` | `outputs/video-jobs` | 视频任务未指定输出路径时的默认目录 |

资源监控默认每 5 秒写入一次 SQLite，保留最近 90 天；页面可切换 `15m`/`1h`/`24h`/`1周`/`1月`，通过前方的周期导航查看更早的连续时段或自然日、周、月，长时间范围由服务端聚合后返回。每张 GPU 的核心负载、频率、功率和温度使用对齐曲线与联动指针显示，显存容量单独展示。内存中只保留最近 15 分钟，不会因长期历史持续占用大量内存。

局域网模式没有 HTTPS，账号密码会以未加密 HTTP 传输，只适合可信局域网，不要直接暴露到公网。

侧栏“文件服务”页面使用登录态浏览配置根目录；目录可以逐级进入，可把一个或多个文件上传到当前目录，也可更名当前目录中的文件或目录，或在确认后将项目移入 Windows 回收站；普通文件点击下载，音视频点击后直接播放。上传、更名和删除需要登录，独立的 `18765` HTTP 接口保持只读、无需登录，供可信局域网中的播放器或下载工具直接访问，因此不要把该端口暴露到公网。视频生成仅给出素材名称时，默认从 `file_service_root/输入/` 查找视频或图片。

## 随系统启动

安装登录后启动任务：

```powershell
.\Install-ManagerTask.ps1
```

安装系统启动任务并指定配置：

```powershell
.\Install-ManagerTask.ps1 -Trigger Startup -ConfigFile .\config\settings.json
```

安装命令需要管理员确认，计划任务以当前管理员用户的最高可用权限运行，使已登记脚本可以管理其固定负责的 Windows 服务和端口转发。卸载任务使用 `.\Uninstall-ManagerTask.ps1`。未设置默认场景时，计划任务只启动管理器；设置默认场景后，AXIS 启动时会按现有场景切换规则自动启停对应服务。

## 更多文档

- [脚本接口要求](scriptspec.md)
- [开发文档、完整 API 与高级配置](DEVELOPMENT.md)
- [English README](README.md)
