# AXIS Development Guide

English | [简体中文](DEVELOPMENT.md)

This guide is for developers, integrations, and troubleshooting. Start with [README.md](README.md) for normal use and [Script Requirements](SCRIPT_REQUIREMENTS.en.md) for the service-script contract.

## Development environment

The following install and test commands apply only to a source checkout containing `requirements-dev.txt` and `tests/`; they do not work from a release package:

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test tests/frontend_request_guard.test.js tests/frontend_gpu_layout.test.js tests/frontend_monitor_chart.test.js tests/frontend_theme.test.js tests/frontend_contract.test.js tests/frontend_i18n.test.js tests/documentation_consistency.test.js
```

Release packages contain neither `requirements-dev.txt` nor `tests/`; their development guides are included only as API and deployment references. Create a clean release directory with:

```powershell
.\Build-Release.ps1 -Destination D:\Release\axis-manager
```

## Versioning

`__version__` in `workstation_manager/__init__.py` is the single source of the application version; both the health endpoint and System Settings read this value. Every delivered code commit must increment the semantic version: compatible fixes increment the patch version, backward-compatible features increment the minor version, and incompatible changes increment the major version.

## API conventions

The API prefix is `/api/v1`; requests and responses use JSON. Error responses keep a stable `error.code` and localize the message from `Accept-Language`.

Initial `auth/setup` is restricted to direct loopback access. After setup, read endpoints other than health, authentication status, and login require a session. Every write except `auth/setup` and `auth/login`, including `auth/logout`, also requires the current session token in `X-CSRF-Token`. The session cookie is `HttpOnly` and `SameSite=Strict`.

Successful endpoints return JSON objects. Create endpoints return `201`, asynchronous actions return `202`, delete endpoints return an empty `204`, and other successful endpoints return `200`. Errors use `{"error":{"code":"...","message":"...","details":...}}`; validation failures return `422`, missing authentication returns `401`, CSRF or source restrictions return `403`, missing targets return `404`, and conflicts or an existing active operation return `409`.

### Request bodies

Initial setup, login, and user creation share one body. After trimming, `username` contains 3..64 characters and `password` contains 4..1024 characters:

```json
{"username":"admin","password":"1234"}
```

Only the login endpoint accepts the optional boolean field `remember`. When `true`, it creates a 30-day server session and a persistent cookie with the same lifetime; omitted or `false` continues to use `session_ttl_seconds`:

```json
{"username":"admin","password":"1234","remember":true}
```

Password updates use:

```json
{"password":"new-password"}
```

Creating or updating a service requires the complete object. `description`, `gpu_label`, and `ui_url` may be empty; `port` may be `null`:

```json
{
  "name": "3090 ComfyUI",
  "description": "Image generation service",
  "script_path": "C:\\Services\\comfyui.ps1",
  "gpu_label": "RTX 3090",
  "port": 8189,
  "ui_url": "http://192.168.100.190:8189/"
}
```

`name` is at most 100 characters, `description` at most 1000, `script_path` is an existing absolute `.ps1`, `.cmd`, or `.bat` path, `gpu_label` is at most 100 characters, `port` is `1..65535`, and `ui_url` is empty or a complete HTTP/HTTPS URL.

Creating or updating a scene uses ordered `service_ids` that contain no unknown service. Duplicate IDs are reduced to their first occurrence. `description` is the card's short introduction with a 1,000-character limit; `detailed_description` is a separate detailed usage field with an 8,000-character limit:

```json
{"name":"Development","description":"Development services","detailed_description":"API Base: http://127.0.0.1:8080/v1","service_ids":["service-id-1","service-id-2"]}
```

Scene reorder `scene_ids` must contain every existing scene ID exactly once:

```json
{"scene_ids":["scene-id-1","scene-id-2"]}
```

A single-service action uses `{"action":"start"}`; `action` is one of `start`, `stop`, or `restart`. Other POST/DELETE actions require no body.

### Health and authentication

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Manager health |
| GET | `/api/v1/auth/status` | Setup and authentication status |
| POST | `/api/v1/auth/setup` | Create the initial administrator from loopback |
| POST | `/api/v1/auth/login` | Sign in and obtain a CSRF token |
| POST | `/api/v1/auth/logout` | End the current session |
| GET | `/api/v1/auth/me` | Current user and a refreshed CSRF token |

### Users and resources

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/users` | List users |
| POST | `/api/v1/users` | Add a user from the manager's local computer |
| PUT | `/api/v1/users/{id}/password` | Change a password and revoke existing sessions |
| DELETE | `/api/v1/users/{id}` | Delete another user who is not the last user |
| GET | `/api/v1/snapshot` | Current host, Docker, and GPU snapshot |
| GET | `/api/v1/history` | Resource history using the `window` query parameter |
| GET | `/api/v1/host-services` | Host-service summary |

### Registered services

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/registered-services` | List registered services |
| GET | `/api/v1/services` | Compatibility alias for registered services |
| POST | `/api/v1/registered-services` | Register a service |
| PUT | `/api/v1/registered-services/{id}` | Update registration fields |
| DELETE | `/api/v1/registered-services/{id}` | Remove a registration and its scene references |
| POST | `/api/v1/registered-services/{id}/status` | Run the script's `status` action once |
| POST | `/api/v1/registered-services/{id}/actions` | Submit `start`, `stop`, or `restart` |
| POST | `/api/v1/registered-services/actions/stop-all` | Create a stop-all operation |

Registration payload fields are `name`, `description`, `script_path`, `gpu_label`, `port`, `ui_url`, `health_url`, and `health_expect`. `health_url` accepts only local-loopback HTTP or HTTPS. `health_expect` may be empty; otherwise the response body must contain that text.

Reading service lists, reloading the page, and starting AXIS never run script `status` actions. AXIS checks health URLs directly inside the manager process every five seconds with a one-second timeout and concurrency limit of two; two consecutive failures are required to change a stable state. Background checks never launch PowerShell, WSL, Docker CLI, or another child process. The `status` endpoint is a user-triggered deep check.

When a health endpoint is unreachable, AXIS combines the result with desired state: a service desired to be stopped remains `stopped`, while one desired to be running becomes `unhealthy`. Immediate verification after an action uses the new desired state just written by that action rather than the cached pre-action value, so a successful stop followed by a health timeout is recorded as `stopped`. This avoids timeout false alarms when a Windows port-forward listener remains present after its backend has stopped.

### Scenes and operation records

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/scenes` | List scenes and computed states |
| POST | `/api/v1/scenes` | Create a scene |
| POST | `/api/v1/scenes/reorder` | Save scene-card order |
| PUT | `/api/v1/scenes/{id}` | Update a scene and service order |
| DELETE | `/api/v1/scenes/{id}` | Delete a scene without controlling services |
| PUT | `/api/v1/scenes/{id}/default` | Make a scene the project's unique default |
| DELETE | `/api/v1/scenes/{id}/default` | Clear the scene's default setting |
| POST | `/api/v1/scenes/{id}/activate` | Create a scene-switch operation |
| GET | `/api/v1/operations` | List operations |
| GET | `/api/v1/operations/{id}` | Read operation steps and results |
| POST | `/api/v1/operations/{id}/cancel` | Cancel steps that have not started |
| GET | `/api/v1/audit` | Read audit events |

Action endpoints return asynchronous operations. The frontend uses operation details to show progress. Cancellation does not undo completed service actions. Setting a default scene does not switch immediately; on its next startup the manager submits the normal scene activation as `system/startup`. Startup controls no services when no default is configured.

### Query parameters and primary responses

- `/api/v1/history` uses a minute-formatted `window`, defaults to `15m`, and accepts `1m..1440m`. `15m` returns raw samples, `1h` uses 15-second buckets, and `24h` uses 60-second buckets. In addition to `samples`, the response still includes `bucket_seconds`, `retention_minutes`, `stored_sample_count`, `stored_since`, and `stored_until` so clients can determine historical coverage; the current resource-monitor UI does not display this metadata.
- Host history includes CPU load/frequency/temperature, physical/committed/page-file memory, primary physical-adapter traffic, and WSL memory/swap. `gpus` also stores memory-controller and encoder/decoder utilization, while `disks` stores per-physical-disk throughput and average latency. GPU P-State, fan, PCIe, clock-limit reasons, process ownership, and Docker container resources are live-snapshot data only and are not persisted.
- If resource-history persistence fails, `/api/v1/health` returns `status: "degraded"`, `readiness.resource_history: "degraded"`, and a `history_persistence_error` without the underlying cause. A health-monitor loop failure adds `service_health_monitor_error` and marks `readiness.registered_services` as `degraded`. Live snapshots remain available while background tasks retry.
- `/api/v1/operations` and `/api/v1/audit` use `limit`, default 100, range `1..500`, and return `operations` or `events` arrays respectively.
- Login and setup return `authenticated`, `csrf_token`, and `expires_at`; `auth/me` returns `username`, `expires_at`, and a new `csrf_token`.
- Service lists return `{"services":[...],"status_mode":"health"}`. Each service includes `desired_state` and a `status` observation with `state`, `checked_at`, `error`, and `source`. Scene lists return `{"scenes":[...]}`. Create and update endpoints return the complete resulting object.
- Service actions, stop-all, and scene activation return `{"operation_id":"32-character hexadecimal ID","status":"queued"}`. A successful cancel request returns the same ID and `cancellation_requested`; operation details contain the operation state and step records.

## Configuration loading

Precedence is environment variables, the JSON file selected by `WM_CONFIG_FILE`, then built-in defaults. See `config/settings.example.json` for the complete JSON example.

Common fields are documented in the README. Development, deployment, and advanced limits use these environment variables:

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

This list follows `workstation_manager/config.py`. Boolean values use `true/false`; list values use JSON or comma-separated input as required by the configuration parser. Do not commit deployment configuration containing local addresses, user data, or credentials.

`WM_ALLOWED_PUBLIC_ORIGINS` and `WM_TRUSTED_PROXY_IPS` are currently parsed and stored only. Runtime code performs no Origin validation or trusted-proxy processing, so these settings are not a security boundary and do not make forwarded client addresses trustworthy. A reverse proxy and firewall must enforce their own source restrictions; AXIS continues to use the direct TCP peer address.

## Data and concurrency

The default database is `data/workstation-manager.db`. The current schema is 20 and migrates automatically at startup. Schema 19 adds the unique scene `is_default` marker. Schema 20 adds the separate scene `detailed_description` field. Existing details remain unchanged when an older client updates a scene without sending that field. Only one manager instance may use a database at a time, preventing duplicate script execution.

The service control plane stores desired and observed states separately. Scenes, the overview, and GPU service summaries use only observed state. SQLite is updated only when the state or error changes, so successful five-second checks do not write continuously. Neither scheduled resource sampling nor health monitoring runs service scripts; only an explicit deep check invokes `status`. Resource sampling writes CPU, memory, and per-GPU load, VRAM, temperature, power, and graphics-clock metrics to SQLite and retains 24 hours by default; the in-memory queue remains limited to the latest 15 minutes.
