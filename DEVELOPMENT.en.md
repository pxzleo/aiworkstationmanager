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

Initial `auth/setup` is restricted to direct loopback access. While the frontend checks the session it shows only the AXIS startup screen, not the login form; the login UI appears only after an unauthenticated result or a failed check. After setup, read endpoints other than health, authentication status, and login require a session. Every write except `auth/setup` and `auth/login`, including `auth/logout`, also requires the current session token in `X-CSRF-Token`. The session cookie is `HttpOnly` and `SameSite=Strict`.

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
  "ui_url": "http://192.168.100.190:8189/",
  "wsl_portproxy_enabled": false,
  "wsl_distro": "Ubuntu-22.04",
  "wsl_listen_address": "0.0.0.0",
  "wsl_listen_port": null,
  "wsl_connect_port": null
}
```

`name` is at most 100 characters, `description` at most 1000, `script_path` is an existing absolute `.ps1`, `.cmd`, or `.bat` path, `gpu_label` is at most 100 characters, `port` is `1..65535`, and `ui_url` is empty or a complete HTTP/HTTPS URL. When `wsl_portproxy_enabled` is enabled, the manager reconciles all declared Windows `portproxy` entries at manager startup and before any service starts or restarts, and removes the old mapping when the registration is disabled, changed, or deleted. It only updates or removes a target recorded by its own previous successful synchronization and refuses to operate on unknown mappings. Success also requires IP Helper to own the actual listener. The listen address must be `0.0.0.0`, loopback, or a private IPv4 address. A missing listen port defaults to the service port, and a missing WSL target port defaults to the listen port.

Creating or updating a scene uses ordered `service_ids` that contain no unknown service. Duplicate IDs are reduced to their first occurrence. `description` is the card's short introduction with a 1,000-character limit; `detailed_description` is a separate detailed usage field with an 8,000-character limit. `is_default_generation` marks the single default generation scene and is selected by the user in the scene editor:

```json
{"name":"Video generation","description":"Video services","detailed_description":"ComfyUI: http://127.0.0.1:8189","is_default_generation":true,"service_ids":["service-id-1","service-id-2"]}
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

Registration payload fields also include `wsl_portproxy_enabled`, `wsl_distro`, `wsl_listen_address`, `wsl_listen_port`, and `wsl_connect_port`. `health_url` accepts only local-loopback HTTP or HTTPS. `health_expect` may be empty; otherwise the response body must contain that text. WSL mappings must be explicitly enabled: the manager never guesses from an ordinary service port and broadens LAN exposure. An unknown mapping, target conflict, synchronization failure, or missing IP Helper listener blocks the affected service startup and is reported by the health endpoint.

Reading service lists and reloading the page never run script `status` actions. AXIS checks health URLs directly inside the manager process every five seconds with a one-second timeout and concurrency limit of two; two consecutive failures are required to change a stable state. Background checks never launch PowerShell, WSL, Docker CLI, or another child process. The `status` endpoint is a user-triggered deep check; startup without a default scene checks registered services sequentially and retries first-pass `unknown` results once after the full pass. A failed lifecycle action runs one additional check to reconcile desired state with reality.

When a health endpoint is unreachable, AXIS combines the result with desired state: a service desired to be stopped remains `stopped`, while one desired to be running becomes `unhealthy`. If desired state is unknown but the latest explicit observation is `unhealthy`, an unreachable or timed-out lightweight probe returning `unknown` preserves that more specific result. Immediate verification after an action uses the new desired state just written by that action rather than the cached pre-action value, so a successful stop followed by a health timeout is recorded as `stopped`. This avoids timeout false alarms when a Windows port-forward listener remains present after its backend has stopped.

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

### Video jobs

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/v1/video-jobs` | Submit a persistent video job from loopback without authentication; repeated `idempotency_key` values are idempotent |
| POST | `/api/v1/video-job-batches` | Atomically submit an ordered loopback-only `workflows` batch; release resources after every segment and callback once |
| GET | `/api/v1/video-jobs` | List jobs for an authenticated user; `limit` defaults to 100 and ranges from `1..500` |
| GET | `/api/v1/video-jobs/{id}` | Read one job, phase, `prompt_id`, progress, and output |
| POST | `/api/v1/video-jobs/{id}/cancel` | Request cancellation of a queued or running job |

### Automatic tasks

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/v1/automatic-tasks` | Page through tasks and counts as an authenticated user; `limit` is `1..500`, `offset` starts at 0, and the response includes `has_more` |
| POST | `/api/v1/automatic-tasks` | Create a task with CSRF; the body contains only `content` |
| POST | `/api/v1/automatic-tasks/reorder` | Save execution order with CSRF; `previous_task_ids` is the order read by the page and `task_ids` is the new order; both must contain every pending task exactly once, and a changed prior order returns a conflict |
| PUT | `/api/v1/automatic-tasks/{id}` | Edit a non-running task with CSRF and reset it to `pending` |
| DELETE | `/api/v1/automatic-tasks/{id}` | Delete a non-running task with CSRF |
| POST | `/api/v1/automatic-tasks/{id}/reset` | Requeue a task with CSRF and invalidate its previous execution token |
| POST | `/api/v1/automatic-tasks/claim` | Atomically claim the first pending task in the saved queue order for the current local OpenCode `session_id` |
| POST | `/api/v1/automatic-tasks/{id}/heartbeat` | Renew the owning local OpenCode session's lease with its `execution_token` |
| POST | `/api/v1/automatic-tasks/{id}/finish` | Idempotently save the terminal state with the owning `execution_token`; failures require a `summary` |

The UI accepts task instructions only; AXIS generates a compact title from the first meaningful sentence. It loads every page using immutable creation order, then displays tasks in execution order. Pending tasks can be moved up or down; the server atomically compares the order originally read by the page and the complete pending set, rejecting concurrent changes explicitly. OpenCode claims work in the successfully saved order, while a running task that is manually requeued is appended to the pending tail. After a claim, the Skill does not stop to ask the user questions or wait for human intervention. Non-critical choices that do not expand authorization, change the task goal or key artifact, or cause irreversible effects use existing project conventions, defaults, and the minimal viable approach, and are recorded in the result summary. Information available from the workspace, existing configuration, or safe read-only checks must be resolved autonomously. A task may fail only when indispensable credentials, assets, or external conditions are missing after safe bounded retries; its instructions conflict internally; a key goal or artifact specification cannot be resolved from conventions, configuration, and read-only checks; objective acceptance criteria remain unmet after reasonable attempts; or new authorization for a high-impact action is required. Unattended execution does not expand authorization for deletion, publication, payment, or external messages. The `axis-automatic-tasks` Skill uses claim, heartbeat, and finish tools to execute every pending task serially in the current OpenCode session. After a claim, the plugin renews the 30-minute lease every minute in the background, including while a long-running tool is active, a single response has failed, or the session is idle waiting for AXIS video generation and its callback. `session.idle`, `status=idle`, and a single `session.error` do not end an automatic-task lease. Network and transient server errors keep retrying; definitive task-not-found, expired-lease, or owner-mismatch responses stop background renewal. An expired claim is recovered by the next claim transaction and its old token cannot write a late result. It claims the next item only after recording the current result, and a failed item records completed checks and its cause before the queue continues. Running items cannot be edited or deleted, but an administrator can requeue them; a second OpenCode session cannot claim work concurrently. Run `integrations/opencode/Install-AxisAutomaticTasks.ps1`, restart OpenCode, and ask it to start automatic tasks.

### HTTP file service

The manager process also starts a standalone HTTP file service on `file_service_port` (default `18765`), bound to the same `host`, with `file_service_root` (default `D:/共享/`) as its root. The standalone service exposes `GET /health`, `GET /api/v1/files?path=&sort_by=&sort_order=`, and `GET /api/v1/files/content?path=&download=`. It does not use the AXIS session and is intended only for direct players and download tools on a trusted LAN; never expose it to the public internet. The manager port exposes these authenticated same-origin endpoints for the UI:

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/v1/file-service` | Return the port, root, and root availability |
| GET | `/api/v1/file-service/files` | List the root or optional `path`; `sort_by` accepts `modified`, `name`, or `size`, `sort_order` accepts `asc` or `desc`, and the default is newest modified first with folders grouped first |
| GET | `/api/v1/file-service/content` | Stream the required `path`; `download=true` returns an attachment |
| POST | `/api/v1/file-service/upload` | Upload the raw request body to optional directory `path` with required file `name`; requires a signed-in administrator and CSRF, and rejects conflicts without overwriting |
| POST | `/api/v1/file-service/rename` | Rename the file or folder at `path` to `new_name` in the same directory with authentication and CSRF; path separators, root escape, and overwrite conflicts are rejected |
| DELETE | `/api/v1/file-service/entry` | Move the file or complete folder selected by query parameter `path` to the Windows Recycle Bin with authentication and CSRF; deleting the root, an outside target, or an upload temporary file is rejected |

Path values use `/` separators relative to the root and support UTF-8 names. After resolving symbolic links and normalizing the path, the service verifies that the target remains inside the root and rejects every path or link whose final target escapes it. Files are streamed from disk and support HTTP Range requests for large downloads and media seeking. Upload is available only on the authenticated manager port: it bypasses the generic 64 KiB JSON-body buffer, streams 1 MiB batches into a temporary file in the target directory, and atomically publishes with a hard link so an existing name is never overwritten. The standalone port 18765 remains read-only. The page defaults to thumbnail view and can switch to list view; thumbnail mode uses a `9:16` portrait frame, previews images and videos, fits the complete uncropped video frame with `object-fit: contain`, and uses type icons for other entries. At viewports up to 820px, thumbnails use a two-column layout. Thumbnails and file names are the only per-item controls: folders enter, ordinary files download, and audio or video opens the player when clicked. Rows no longer show redundant open, download, or play buttons; videos request fullscreen.

The submission body references resources already prepared by OpenCode; AXIS does not create prompts, reference images, audio, or workflows:

```json
{
  "idempotency_key": "project-session-video-001",
  "session_id": "ses_xxx",
  "workflow_path": "D:\\AIWork\\job\\h3-api-workflow.json",
  "workflow_file_sha256": "<64-character lowercase SHA-256>",
  "output_path": "D:\\AIWork\\job\\final.mp4",
  "scene_name": "Video generation",
  "callback_url": "http://127.0.0.1:61714",
  "callback_directory": "D:\\AIWork\\job"
}
```

`workflow_path` must be an existing absolute JSON path containing the ComfyUI API workflow `prompt` object. Optional `workflow_file_sha256` is the original file's 64-character lowercase SHA-256; AXIS verifies it while reading and snapshotting the same bytes. Its contents are snapshotted into the persistent job at submission, so later file changes cannot alter queued work. `output_path` is optional; when supplied it must be absolute and existing files are never overwritten, otherwise output is saved below `video_output_directory/<job_id>/`. `scene_name` may name an existing generation scene; when omitted, AXIS uses the single default generation scene selected in scene settings. Video data is streamed into a same-directory temporary file and atomically renamed. `callback_url` must be a loopback base without credentials, path, query, or fragment, and `callback_directory` can preserve the original OpenCode working directory. Submission and callback carry no authentication information. After cleanup AXIS calls the OpenCode plugin's loopback bridge with up to three bounded retries. Listing and cancellation continue to use the AXIS management UI session and CSRF controls.

Multi-segment video uses `/api/v1/video-job-batches`, where `workflows` contains 1..100 ordered `{workflow_path, workflow_file_sha256, output_path}` objects. AXIS persists `batch_id`, `batch_index`, and `batch_size`, calls ComfyUI `/free` after every segment, avoids scene restoration and callbacks between segments, and sends one callback at batch completion or first failure. The job-list response also includes authoritative `queue_summary`; `queued_segments` counts only jobs whose status is `queued`.

Each successful video segment keeps its original output and atomically publishes a copy to `file_service_root/video-jobs/<job ID>/<filename>`. Job-list responses expose `shared_output_path` for a published copy, and the UI renders the original full output path as a clickable same-origin file-service link. OpenCode completion callbacks receive a direct `http://127.0.0.1:<file_service_port>/api/v1/files/content?path=...` URL. Copy failures or conflicting content fail explicitly without overwriting, and restart recovery idempotently resumes publication from an already collected original output.

`integrations/opencode/plugins/axis-video.ts` registers the single-job `axis_video_submit` and multi-segment `axis_video_submit_batch` tools, obtains the current `sessionID` and directory automatically, and creates an unauthenticated callback bridge on a random loopback port. It resumes the original session through OpenCode's internal client when AXIS reports the consolidated result. A cancellation callback defines cancellation as terminal and explicitly forbids OpenCode from automatically regenerating, resubmitting, or continuing later batch segments; only a new explicit generation request made by the user after cancellation permits a new job. A cancellation accepted before final callback delivery overrides either a successful or failed outcome; once the job enters `callback_pending`, cancellation is rejected and the UI hides its cancellation control, providing a deterministic cutoff. The project keeps both the `axis-video` scheduling Skill and the `h3-ref2v-video-pipeline` workflow Skill under `integrations/opencode/skills/`; the latter includes sanitized 4/8-step baselines plus API-graph build, direct-submission, and finishing scripts. Run `integrations/opencode/Install-AxisVideo.ps1` to install the plugin and both Skills for the current user, restart OpenCode, then enter `使用场景切换技能生成视频` to trigger it.

When a generation request provides only an asset name and no download URL or absolute path, both Skills call `h3-ref2v-video-pipeline/scripts/resolve_shared_input.py` and resolve only images or videos from `file_service_root/输入/`. Pass `--root` for a non-default root. The name may be an exact file name or a unique stem; directory components, non-media files, missing names, and multiple matches fail explicitly. `build_api.py --source` automatically applies the same video-only resolution to a value containing only a file name.

The plugin tracks readiness separately for each job or batch and exposes `GET /session/{session_id}/handoff_ready/{job_or_batch_id}` to AXIS. It returns `425` while the OpenCode session is `busy` or `retry`, then returns `204` after `session.idle` or an `idle` status. A completion callback or ordinary user message starts a new response and blocks every other pending handoff in the same session again, so concurrent jobs cannot bypass the guard. AXIS checks NInfer idleness and switches to the video scene only after that `204`; a handoff timeout leaves NInfer running and fails with `opencode_handoff_timeout`. A legacy plugin that does not implement this route and returns `404` or `405` remains compatible.

The scheduler first acquires the exclusive RTX 4090 lease and persists the currently active original scene so no new manual scene switch can begin while it waits, then checks both NInfer `/slots` and `/metrics`. It activates the requested generation scene, or the default generation scene when none is named, only after every slot is idle and both `requests_processing` and `requests_deferred` are zero. It then verifies ComfyUI `/system_stats`, submits `/prompt`, stores `prompt_id`, connects to `/ws` with the job-specific `client_id` for prompt-scoped current-node and real `value/max` sampling progress, continues polling `/queue` and `/history/{prompt_id}` as completion, failure, and disconnect fallback, and collects video through `/view`. Success, failure, and cancellation all restore the persisted original scene before asynchronously notifying the original `session_id`. Non-terminal jobs recover after an AXIS restart. If restart occurs between ComfyUI acceptance and durable `prompt_id` storage, AXIS recovers only through the `axis_job_id` marker in queue/history; an indeterminate submission fails explicitly and is never submitted twice.

Action endpoints return asynchronous operations. The frontend uses operation details to show progress. Cancellation does not undo completed service actions. Setting a default scene does not switch immediately; on its next startup the manager submits the normal scene activation as `system/startup`. Startup controls no services when no default is configured, but it runs each read-only `status` action, retries first-pass `unknown` results once, and synchronizes explicit `running`/`stopped` results into desired state; `unhealthy`/`unknown` map desired state to `unknown`.

### Query parameters and primary responses

- `/api/v1/history` uses a minute-formatted `window`, defaults to `15m`, and accepts `1m..1440m`. `15m` returns raw samples, `1h` uses 15-second buckets, and `24h` uses 60-second buckets. In addition to `samples`, the response still includes `bucket_seconds`, `retention_minutes`, `stored_sample_count`, `stored_since`, and `stored_until` so clients can determine historical coverage; the current resource-monitor UI does not display this metadata.
- Host history includes CPU load/frequency/temperature, physical/committed/page-file memory, primary physical-adapter traffic, and WSL memory/swap. `gpus` also stores memory-controller and encoder/decoder utilization, while `disks` stores per-physical-disk throughput and average latency. GPU P-State, fan, PCIe, clock-limit reasons, process ownership, and Docker container resources are live-snapshot data only and are not persisted.
- If resource-history persistence fails, `/api/v1/health` returns `status: "degraded"`, `readiness.resource_history: "degraded"`, and a `history_persistence_error` without the underlying cause. A health-monitor loop failure adds `service_health_monitor_error` and marks `readiness.registered_services` as `degraded`. Live snapshots remain available while background tasks retry.
- `/api/v1/operations`, `/api/v1/audit`, and `/api/v1/video-jobs` use `limit`, default 100, range `1..500`, and return `operations`, `events`, or `jobs` arrays respectively.
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
WM_FILE_SERVICE_PORT
WM_FILE_SERVICE_ROOT
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
WM_COMFYUI_BASE_URL
WM_NINFER_BASE_URL
WM_NINFER_MODEL_ID
WM_VIDEO_OUTPUT_DIRECTORY
WM_VIDEO_JOB_POLL_INTERVAL_SECONDS
WM_VIDEO_JOB_IDLE_TIMEOUT_SECONDS
WM_VIDEO_JOB_SCENE_TIMEOUT_SECONDS
WM_VIDEO_JOB_GENERATION_TIMEOUT_SECONDS
```

This list follows `workstation_manager/config.py`. Boolean values use `true/false`; list values use JSON or comma-separated input as required by the configuration parser. Do not commit deployment configuration containing local addresses, user data, or credentials.

`WM_ALLOWED_PUBLIC_ORIGINS` and `WM_TRUSTED_PROXY_IPS` are currently parsed and stored only. Runtime code performs no Origin validation or trusted-proxy processing, so these settings are not a security boundary and do not make forwarded client addresses trustworthy. A reverse proxy and firewall must enforce their own source restrictions; AXIS continues to use the direct TCP peer address.

## Data and concurrency

The default database is `data/workstation-manager.db`. The current schema is 31 and migrates automatically at startup. Schema 19 adds the unique scene `is_default` marker. Schema 20 adds the separate scene `detailed_description` field. Schema 21 adds the authoritative operation `total_steps` count. Schema 22 adds explicit WSL `portproxy` configuration to registered services. Schema 23 adds the legacy scene-purpose fields, the persistent `video_jobs` state machine, and RTX 4090 `resource_leases`. Schema 24 removes the video-job callback authorization field. Schema 25 migrates the legacy `video_gen` purpose into the unique `is_default_generation` checkbox and stores each video job's generation and original scenes. Schema 26 adds explicit video batch IDs, segment indices, and segment counts. Schema 27 persists each segment's workflow-derived video specification so job listings do not repeatedly parse full workflows. Schema 28 adds the source-video title or prompt summary to that specification and backfills existing jobs. Schema 29 persists the relative output path successfully published to the shared file service. Schema 30 adds the automatic-task queue, execution-session ownership, and result status. Schema 31 adds a persistent pending-task order. Existing details and default-generation values remain unchanged when an older client omits those fields. Only one manager instance may use a database at a time, preventing duplicate script execution.

The service control plane stores desired and observed states separately. Scenes, the overview, and GPU service summaries use only observed state. SQLite is updated only when the state or error changes, so successful five-second checks do not write continuously. Neither scheduled resource sampling nor health monitoring runs service scripts; explicit deep checks, startup reconciliation without a default scene, and failed-action reconciliation invoke `status`. Resource sampling writes CPU, memory, and per-GPU load, VRAM, temperature, power, and graphics-clock metrics to SQLite and retains 24 hours by default; the in-memory queue remains limited to the latest 15 minutes.
