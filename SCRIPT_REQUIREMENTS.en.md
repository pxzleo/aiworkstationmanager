# Registered Service Management Script Requirements

English | [简体中文](脚本要求.md)

This document defines the script interface required by AXIS Workstation Manager for registered services.

## 1. Basic requirements

- Each registered service provides one independent management script.
- Supported formats are `.ps1`, `.cmd`, and `.bat`.
- Registration requires an absolute Windows path, for example:

  ```text
  D:\AI\MyService\manage.ps1
  ```

- AXIS runs the script with the script's directory as the working directory. A script may resolve its configuration relative to that directory, although explicit paths derived from the script directory are recommended.
- AXIS passes exactly one action argument. GPU labels, ports, UI URLs, and other registration fields are not passed to the script.

## 2. Action interface

The script must accept all four actions:

| Action | Purpose | Expected state after success |
|---|---|---|
| `start` | Start the service | `running` |
| `stop` | Stop the service | `stopped` |
| `restart` | Restart the service | `running` |
| `status` | Inspect the service | One of the four defined states |

PowerShell invocation:

```powershell
manage.ps1 start
manage.ps1 stop
manage.ps1 restart
manage.ps1 status
```

Batch invocation:

```bat
manage.cmd start
manage.cmd status
```

An unknown action must return a non-zero exit code and a clear error message.

## 3. Strict `status` contract

`status` must:

- Be read-only. It must not start, stop, restart, or modify the service.
- Complete quickly. The default timeout is 3 seconds.
- Return exit code `0` on success.
- Print exactly one lowercase state to standard output:

  ```text
  running
  stopped
  unhealthy
  unknown
  ```

State meanings:

- `running`: the service is running and usable.
- `stopped`: the service is definitely not running.
- `unhealthy`: the process exists, but its health check fails or it cannot provide its function.
- `unknown`: the script cannot determine the state reliably.

Do not add prefixes, explanations, JSON, logs, or other text. `status: running`, `RUNNING`, and localized status words are invalid. A normal trailing newline is allowed.

A non-zero exit code, timeout, or invalid output is stored as `unknown`.

AXIS never polls `status`. Starting AXIS, opening or reloading the page, and reading the service list do not execute scripts. AXIS invokes `status` once only when a user clicks Check Status for one service. A service's manual status check and lifecycle actions execute serially.

## 4. `start`, `stop`, and `restart`

- Return exit code `0` only on success.
- Return a non-zero exit code on failure and preferably write a short, specific cause to standard error.
- The default lifecycle-action timeout is 600 seconds.
- Do not remain attached as a permanent foreground process:
  - `start` starts the background service, waits until it is usable, and exits.
  - `stop` waits until the service has actually stopped and exits.
  - `restart` completes both stop and start, waits until the service is usable, and exits.
- A background service started by the script must not inherit the management script's standard-output or standard-error handles. Redirect background output to a service-owned log or a null device; inherited handles can keep the management action open indefinitely.
- AXIS does not call `status` after a lifecycle action. Exit code 0 from `start` or `restart` stores `running`; exit code 0 from `stop` stores `stopped`; a non-zero exit stores `unknown`.
- Return success only after the real service has reached the intended state. AXIS restart and page reload restore the stored state without probing the service.

## 5. Idempotency

Every action must be safe to repeat:

- `start` on an already running service keeps it running and succeeds.
- `stop` on an already stopped service keeps it stopped and succeeds.
- `restart` handles running, stopped, and unhealthy states.

Repeated scene switches may call `start` again for target services. A service already being in the target state must not be treated as an error.

## 6. Output and errors

- UTF-8 is recommended for localized error output.
- `status` standard output contains only the defined state.
- Lifecycle actions may print short progress messages, but AXIS does not store them as service logs.
- Failure output should identify the root cause, for example:

  ```text
  The service did not listen on port 8000 within 60 seconds
  D:\AI\MyService\config.json was not found
  Stop failed: process 1234 is still running
  ```

- AXIS keeps at most the last 4,096 characters of standard output and standard error for a failure summary. Do not emit large logs.
- AXIS provides no `logs` action and never reads or proxies service-owned logs.

## 7. PowerShell example

This example shows only the interface. Replace the lifecycle and health logic with the real service implementation.

```powershell
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$serviceName = "MyService"

try {
    switch ($Action) {
        "start" {
            Start-Service -Name $serviceName
            exit 0
        }
        "stop" {
            Stop-Service -Name $serviceName
            exit 0
        }
        "restart" {
            Restart-Service -Name $serviceName
            exit 0
        }
        "status" {
            $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
            if ($null -eq $service) {
                [Console]::Out.WriteLine("unknown")
            }
            elseif ($service.Status -eq "Running") {
                [Console]::Out.WriteLine("running")
            }
            elseif ($service.Status -eq "Stopped") {
                [Console]::Out.WriteLine("stopped")
            }
            else {
                [Console]::Out.WriteLine("unhealthy")
            }
            exit 0
        }
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
```

## 8. Batch example

```bat
@echo off
setlocal

if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="restart" goto restart
if /I "%~1"=="status" goto status

>&2 echo Unsupported action: %~1
exit /b 2

:start
rem Start the background service and wait until it is usable.
exit /b 0

:stop
rem Stop the service and wait until it has fully exited.
exit /b 0

:restart
call "%~f0" stop || exit /b 1
call "%~f0" start || exit /b 1
exit /b 0

:status
rem Inspect the real process, port, or health endpoint. This is only a placeholder.
echo unknown
exit /b 0
```

## 9. Registration fields versus script responsibilities

The following values are entered by the user for display or navigation only. AXIS does not pass them to the script or verify them automatically:

- Service name
- Service description
- GPU display label
- Service port
- UI URL

The script owns real lifecycle control and health evaluation. GPU selection, process management, port checks, dependency checks, and service-specific logging belong in the script.

## 10. Pre-registration checklist

Before registration, test the script from an ordinary PowerShell or Command Prompt window:

1. Run `status` by absolute path and confirm it prints exactly one defined state within 3 seconds.
2. Run `start`, confirm the script exits, and then confirm `status` prints `running`.
3. Run `start` again and confirm repeated startup succeeds.
4. Run `restart` and confirm the service returns to `running`.
5. Run `stop`, confirm the script exits, and then confirm `status` prints `stopped`.
6. Run `stop` again and confirm repeated stop succeeds.
7. Force one startup failure and confirm the script returns a non-zero exit code with a clear cause.
