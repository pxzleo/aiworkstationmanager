# AXIS AI Workstation Manager

English | [简体中文](README.zh-CN.md)

AXIS unifies the services of an AI workstation and organizes them into different scenes. It can switch all required services for a scene with one action while monitoring server status in real time. Its responsive UI supports monitoring and configuration from PC to mobile, and it can accommodate any service: ask AI to write or adapt an existing management script to the [scriptspec.md](scriptspec.md) contract. AXIS is a clean and straightforward service-management system for AI workstations.

## Interface preview

**Workstation overview**

![AXIS workstation overview showing the active scene, host status, and dual-GPU activity](docs/1.png)

**Work scenes**

![Work scene management showing service order, status, and scene-switch actions](docs/2.png)

**Registered services**

![Registered services showing descriptions, GPUs, ports, states, and management actions](docs/3.png)

**Host resource monitoring**

![Resource monitor showing CPU and memory history charts](docs/4.png)

**GPU core telemetry correlation**

![Full-width correlated charts for GPU core load, clock, power, and temperature](docs/5.png)

## Features

- Register `.ps1`, `.cmd`, or `.bat` service-management scripts
- Start, stop, restart, and deep-check one service, or stop all services
- Detect external starts, stops, failures, and unexpected exits through lightweight local health checks
- Create and reorder scenes that switch an ordered group of services
- Submit, cancel, and monitor every stage of local video generation on a dedicated Video Jobs page
- Switch to a named or default generation scene under an exclusive GPU lease, then restore the original scene
- Monitor CPU, memory, and every detected NVIDIA GPU in distinct sections with consistent scales, current/average/peak/minimum values, and key hardware metrics
- Record service actions and scene-switch steps, times, and results
- Use Chinese, English, or automatic browser-language detection
- Choose Matrix Green, Aurora Blue, or Obsidian Gold display styles
- View the running AXIS version and open the GitHub project from System Settings
- Manage multiple users

## Install and start

Windows and Python 3.11 or later are required.

```powershell
python -m pip install -r requirements.txt
python -m workstation_manager
```

Open:

```text
http://127.0.0.1:19100
```

Create the initial administrator on the first visit. Passwords must contain at least four characters. You can also double-click `Start-Manager.cmd` or run `Start-Manager.ps1`.

## Register a service

Open Registered Services and enter a name, description, and absolute management-script path. GPU label, port, and UI URL are display and navigation fields. A health-check URL and optional response match text let AXIS confirm the real service state automatically.

Every script must support four actions:

```powershell
D:\AIWork\example\manage.ps1 start
D:\AIWork\example\manage.ps1 stop
D:\AIWork\example\manage.ps1 restart
D:\AIWork\example\manage.ps1 status
```

AXIS never polls management scripts and never launches PowerShell, WSL, or Docker commands for background status monitoring. Every five seconds it checks registered local health URLs inside the manager process and changes a stable state only after two consecutive failures. AXIS runs `status` for a user-requested Deep Check, when it starts without a default scene, and after a failed lifecycle action so stale desired state cannot survive the failure. A service that remains `unknown` after the first startup pass receives one read-only retry after the full pass, allowing cold runtime environments such as WSL to become ready.

See [Script Requirements](SCRIPT_REQUIREMENTS.en.md) for the full contract and examples.

## Use scenes

Create a scene in Work Scenes and select its registered services. Only services assigned to at least one scene participate in scene switching. Registered services that belong to no scene remain monitored and manually controllable, but scene switching neither controls them nor includes them in scene state. Before switching, AXIS refreshes lightweight observed health, stops only running scene-managed services outside the target, and starts only target services that are not already running. Target startup begins only after every required stop succeeds.

The progress window shows every step and can cancel steps that have not started. Completed service actions are not rolled back automatically.

A project can have one optional default scene. Setting it does not switch immediately; AXIS activates it the next time the manager starts. Clearing the default only removes this startup behavior and does not stop current services; on the next startup AXIS performs read-only reconciliation for every registered service without starting or stopping it.

Scenes no longer have `Code Agent` or `Video Gen` types. The scene editor only adds a single **Default generation scene** checkbox, and at most one scene can be selected. OpenCode submits a ComfyUI API workflow directly to the local `POST /api/v1/video-jobs` endpoint through the bundled `axis_video_submit` tool, with no token, username, password, or authorization header. A request may name its generation scene; otherwise AXIS uses the default generation scene. AXIS persists the job and original active scene, waits until NInfer has no processing or deferred requests, holds an exclusive GPU lease, switches to the generation scene, monitors the ComfyUI `prompt_id`, stores the output, restores the original scene, and finally calls back through the plugin's loopback bridge to the original OpenCode session. AXIS never switches while NInfer is busy. Version 1 accepts submissions only from the local machine; an explicit output path wins, otherwise the configured default directory is used.

Install the OpenCode integration, then restart OpenCode. The installer deploys the bundled `axis-video` scheduling Skill, the `h3-ref2v-video-pipeline` workflow Skill with sanitized 4/8-step baselines and its build/submission/finishing scripts, and the `axis-video` plugin:

```powershell
.\integrations\opencode\Install-AxisVideo.ps1
```

In OpenCode, enter `使用场景切换技能生成视频` (use the scene-switching skill to generate a video), optionally naming the generation scene in the same request. OpenCode prepares the ComfyUI API workflow and calls `axis_video_submit`; without a name, AXIS uses the default generation scene. The plugin obtains the current session ID and directory automatically, while the AXIS Video Jobs page shows progress, restores the original scene, and returns the final result to the original session.

Automatic tasks use a separate OpenCode integration. Run the installer below and restart OpenCode, add tasks on the AXIS **Automatic Tasks** page, then enter `启动自动任务` (start automatic tasks). OpenCode runs every pending item serially in creation order. The execution lease is renewed periodically, recovers after an unexpected exit, and a running task can also be requeued from the page.

```powershell
.\integrations\opencode\Install-AxisAutomaticTasks.ps1
```

On a private computer or phone, select **Sign in automatically on this device** to stay signed in for 30 days. AXIS never stores the password in the browser; signing out or changing the password still revokes the session immediately.

## Common configuration

Copy the example before changing settings:

```powershell
Copy-Item .\config\settings.example.json .\config\settings.json
.\Start-Manager.ps1 -ConfigFile .\config\settings.json
```

Most installations need only these fields:

| Field | Default | Purpose |
| --- | --- | --- |
| `host` | `127.0.0.1` | Listen address; after initial setup, use `0.0.0.0` for LAN access |
| `port` | `19100` | Manager port |
| `file_service_port` | `18765` | HTTP file-service port started with AXIS |
| `file_service_root` | `D:/共享/` | Root folder available for browsing, download, and playback |
| `database_path` | `data/workstation-manager.db` | Users, services, scenes, and operation records |
| `sample_interval_seconds` | `5` | Resource sampling interval; it never calls service scripts |
| `history_minutes` | `1440` | SQLite resource-history retention in minutes |
| `script_status_timeout_seconds` | `3` | Per-`status` timeout for Deep Check, startup reconciliation, and failed-action reconciliation |
| `script_action_timeout_seconds` | `600` | Service-action timeout |
| `comfyui_base_url` | `http://127.0.0.1:8189` | Local ComfyUI API used for video jobs |
| `ninfer_base_url` | `http://127.0.0.1:8080` | Local NInfer API used for idle checks and recovery verification |
| `video_output_directory` | `outputs/video-jobs` | Default directory when a video job omits an output path |

Resource monitoring writes one SQLite sample every 5 seconds by default and retains the latest 24 hours. The UI supports `15m`, `1h`, and `24h`; longer windows are aggregated by the server before they are returned. For each GPU, aligned charts and a linked pointer compare core load, clock, power, and temperature, while VRAM capacity remains separate. Only the latest 15 minutes remain in memory, so 24-hour history does not create a large in-memory buffer.

LAN mode does not provide HTTPS. Credentials travel over unencrypted HTTP, so use it only on a trusted LAN and never expose it directly to the internet.

The sidebar **File Service** page uses the signed-in session to browse the configured root, enter folders, upload one or more files to the current directory, rename files or folders in place, download ordinary files, and play audio or video inline. Uploads and renames require authentication and never overwrite an existing name. The standalone `18765` HTTP API remains read-only and intentionally unauthenticated for direct access by trusted-LAN players and download tools; never expose that port to the public internet. When video generation receives only an asset name, it resolves images or videos from `file_service_root/输入/` by default.

## Start with Windows

Install a task that runs at sign-in:

```powershell
.\Install-ManagerTask.ps1
```

Install a system-start task with an explicit configuration file:

```powershell
.\Install-ManagerTask.ps1 -Trigger Startup -ConfigFile .\config\settings.json
```

Installation requires administrator confirmation. The task runs as the current administrator with the highest available privileges so registered scripts can manage their fixed Windows services and port proxies. Remove it with `.\Uninstall-ManagerTask.ps1`. With no default scene, the task starts AXIS only. When a default scene is configured, AXIS applies the normal scene-switch rules at startup and controls the corresponding services.

## More documentation

- [Script Requirements](SCRIPT_REQUIREMENTS.en.md)
- [Development, complete API, and advanced configuration](DEVELOPMENT.en.md)
- [中文说明](README.zh-CN.md)
