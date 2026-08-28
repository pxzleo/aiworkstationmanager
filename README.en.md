# AXIS AI Workstation Manager

English | [简体中文](README.md)

AXIS is a local web manager for Windows AI workstations. It starts, stops, and restarts services through user-registered scripts while keeping resource monitoring, scene switching, user management, and operation records in one simple interface.

## Features

- Register `.ps1`, `.cmd`, or `.bat` service-management scripts
- Start, stop, restart, and manually check one service, or stop all services
- Create and reorder scenes that switch an ordered group of services
- Monitor CPU, memory, and every detected NVIDIA GPU in distinct sections with consistent scales, current/average/peak/minimum values, and key hardware metrics
- Record service actions and scene-switch steps, times, and results
- Use Chinese, English, or automatic browser-language detection
- Choose Matrix Green, Aurora Blue, or Obsidian Gold display styles
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

Open Registered Services and enter a name, description, and absolute management-script path. The GPU label, port, and UI URL are optional display fields.

Every script must support four actions:

```powershell
D:\AIWork\example\manage.ps1 start
D:\AIWork\example\manage.ps1 stop
D:\AIWork\example\manage.ps1 restart
D:\AIWork\example\manage.ps1 status
```

AXIS does not continuously poll scripts. A successful start, stop, or restart saves the new state. The `status` action runs only when a user clicks Check Status for one service.

See [Script Requirements](SCRIPT_REQUIREMENTS.en.md) for the full contract and examples.

## Use scenes

Create a scene in Work Scenes and select its registered services. When switching scenes, AXIS first stops services whose saved state is Running and that are not in the target scene. It starts target services in scene order only after every required stop succeeds.

The progress window shows every step and can cancel steps that have not started. Completed service actions are not rolled back automatically.

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
| `database_path` | `data/workstation-manager.db` | Users, services, scenes, and operation records |
| `sample_interval_seconds` | `5` | Resource sampling interval; it never calls service scripts |
| `history_minutes` | `1440` | SQLite resource-history retention in minutes |
| `script_status_timeout_seconds` | `3` | Timeout for a manual single-service status check |
| `script_action_timeout_seconds` | `600` | Service-action timeout |

Resource monitoring writes one SQLite sample every 5 seconds by default and retains the latest 24 hours. The UI supports `15m`, `1h`, and `24h`; longer windows are aggregated by the server before they are returned. Only the latest 15 minutes remain in memory, so 24-hour history does not create a large in-memory buffer.

LAN mode does not provide HTTPS. Credentials travel over unencrypted HTTP, so use it only on a trusted LAN and never expose it directly to the internet.

## Start with Windows

Install a task that runs at sign-in:

```powershell
.\Install-ManagerTask.ps1
```

Install a system-start task with an explicit configuration file:

```powershell
.\Install-ManagerTask.ps1 -Trigger Startup -ConfigFile .\config\settings.json
```

Remove it with `.\Uninstall-ManagerTask.ps1`. The scheduled task starts AXIS only; it never starts registered services automatically.

## More documentation

- [Script Requirements](SCRIPT_REQUIREMENTS.en.md)
- [Development, complete API, and advanced configuration](DEVELOPMENT.en.md)
- [中文说明](README.md)
