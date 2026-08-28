# AXIS AI Workstation Manager

English | [简体中文](README.md)

AXIS is a local web manager for Windows workstations. It samples host, CPU, memory, disk, Docker, and NVIDIA GPU metrics, keeps short-term history, and controls user-registered services through management scripts. Ordered service groups can be saved and switched as scenes.

## Install and start

```powershell
python -m pip install -r requirements.txt
python -m workstation_manager
```

The default address is:

```text
http://127.0.0.1:19100
```

The first visit creates the initial administrator. Passwords must contain at least four characters. AXIS supports multiple users with the same administrative permissions. You can also start it with `Start-Manager.ps1` or `Start-Manager.cmd`.

Install a scheduled task that starts at sign-in:

```powershell
.\Install-ManagerTask.ps1
```

Install a system-start task with an explicit configuration file:

```powershell
.\Install-ManagerTask.ps1 -Trigger Startup -ConfigFile .\config\settings.json
```

Remove the task with `.\Uninstall-ManagerTask.ps1`. Scheduled-task scripts are only run manually; the manager never creates or removes its own scheduled task.

The scheduled task is named `AXIS-AI-Workstation-Manager`. It starts only AXIS and never starts a registered service automatically.

## User management

The User Management page lists every administrator, creation time, active-session count, and the currently signed-in user. Signed-in users can add users, change any user's password, and delete other users. The current user and the last remaining user cannot be deleted.

Changing a password immediately invalidates all sessions for that user. When a user changes their own password, they must sign in again. Creating a user is restricted to requests from the manager's local computer; listing users, changing passwords, and deleting users still require authentication and CSRF validation.

Run tests from a source checkout that contains the `tests/` directory. Release packages do not include the test suite.

```powershell
python -m unittest discover -s tests -v
node --test tests/frontend_request_guard.test.js tests/frontend_contract.test.js tests/frontend_i18n.test.js tests/documentation_consistency.test.js
```

## Registered services

Registered Services is the only service-control model. A service registration contains:

- Name and description
- Absolute path to its management script
- Optional GPU display label
- Optional service port
- Optional complete HTTP or HTTPS UI URL

Scripts may use `.cmd`, `.bat`, or `.ps1` and must accept one fixed action argument. See [Script Requirements](SCRIPT_REQUIREMENTS.en.md) for the complete contract, error behavior, and examples. The [Chinese version](脚本要求.md) is also available.

```powershell
D:\AIWork\example\manage.ps1 start
D:\AIWork\example\manage.ps1 stop
D:\AIWork\example\manage.ps1 restart
D:\AIWork\example\manage.ps1 status
```

`start`, `stop`, and `restart` return exit code 0 on success and a non-zero exit code with a clear error on failure. The script owns dependency setup, GPU selection, port conflict handling, health validation, and idempotency.

`status` must be fast, read-only, and side-effect free. On success it prints exactly one of:

```text
running
stopped
unhealthy
unknown
```

Service states are stored permanently in the database. Starting the manager, reloading the page, and reading service lists never run `status`. A successful `start` or `restart` stores `running`; a successful `stop` stores `stopped`. AXIS runs `status` only when a user clicks Check Status for one service. The default timeout is 3 seconds for `status` and 600 seconds for lifecycle actions.

Deleting a registration removes that service from every scene but does not delete the script, project, model, or underlying service.

Stop All Services invokes `stop` in the current service-list order: case-insensitive service name, then ID. It continues after individual failures and records all results in one operation.

## Scenes

The scene editor creates, edits, deletes, and reorders scenes. Each scene contains an ordered list of registered services.

When switching to a scene, AXIS:

1. Calls `stop` only for services whose stored state is `running` and that are not in the target scene.
2. Does not stop non-target services stored as `stopped`, `unhealthy`, or `unknown`. If any required stop fails, the start phase is skipped.
3. Calls `start` for target services in scene order after all required stops succeed.
4. Continues with later target services if one target fails to start.
5. Does not run a batch `status` check afterward.

A scene is Active when every target service is stored as `running` and no non-target service is stored as `running`. A non-empty scene is Inactive when none of its target services is running, even if a non-target service is still running. It is Partially Started when at least one but not every target service is running, or when every target is running but a non-target service is also running. An empty scene is Active when no service is running and Partially Started otherwise.

Scene switching does not roll back completed actions. Deleting a scene does not stop or delete services. Only one manager instance may use a database at a time, preventing duplicate script execution.

## Resource monitoring and operation logs

The Overview keeps the dual-GPU layout plus CPU, memory, disk, Docker, GPU load, and VRAM metrics. Resource Monitor shows 15 minutes of CPU, memory, per-GPU load, and per-GPU VRAM history.

The Operation Log contains only management operations:

- Service start, stop, and restart
- Scene switches and their service steps
- Operation times, results, and failure summaries

Create, edit, delete, and sign-in audit events are not shown in the Operation Log. AXIS does not read or proxy service-specific logs.

## API

- Health: `GET /api/v1/health`
- Authentication: `GET /api/v1/auth/status`, `POST /api/v1/auth/setup`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- Users: `GET/POST /api/v1/users`, `PUT /api/v1/users/{id}/password`, `DELETE /api/v1/users/{id}`
- Resources: `GET /api/v1/snapshot`, `GET /api/v1/history?window=15m`, `GET /api/v1/host-services`
- Services: `GET/POST /api/v1/registered-services`, `PUT/DELETE /api/v1/registered-services/{id}`, `POST /api/v1/registered-services/{id}/status`, `POST /api/v1/registered-services/{id}/actions`, `POST /api/v1/registered-services/actions/stop-all`
- Scenes: `GET/POST /api/v1/scenes`, `POST /api/v1/scenes/reorder`, `PUT/DELETE /api/v1/scenes/{id}`, `POST /api/v1/scenes/{id}/activate`
- Records: `GET /api/v1/operations`, `GET /api/v1/operations/{id}`, `POST /api/v1/operations/{id}/cancel`, `GET /api/v1/audit`

`GET /api/v1/services` is a compatibility alias for the service list. `health`, `auth/status`, and `auth/login` do not require an existing session. Initial `auth/setup` is restricted to direct loopback access. After setup, all other read endpoints require authentication, and management writes outside the authentication endpoints require the current session's CSRF token. Before setup is complete, local requests may read resource samples to support initial setup.

## Languages

The UI supports Chinese and English. On the first visit, a browser whose preferred language starts with `zh` uses Chinese; every other language uses English. Language selectors are available on both the sign-in page and the main top bar. A manual selection is applied immediately and saved in that browser.

The frontend sends its current language with API requests. Error codes and response structures remain stable while the primary error message is returned in Chinese or English. Older clients that send no language header continue to receive Chinese errors.

## Configuration

See `config/settings.example.json` for every field. Service-script timeouts are:

- `script_status_timeout_seconds`: default 3
- `script_action_timeout_seconds`: default 600

The default database is `data/workstation-manager.db`. The current SQLite schema is 13 and migrates automatically. Legacy script discovery, control adapters, and recovery-lock tables were removed in schema 10.

Create a clean release directory with:

```powershell
.\Build-Release.ps1 -Destination D:\Release\axis-manager
```

The release does not include databases, logs, output, or local service registrations.
