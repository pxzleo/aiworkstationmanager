# 已登记服务管理脚本要求

[English](SCRIPT_REQUIREMENTS.en.md) | 简体中文

本文说明 AXIS 工作站管理器对“已登记服务”管理脚本的接口要求。

## 1. 基本要求

- 每个已登记服务提供一个独立的管理脚本。
- 支持的脚本格式：`.ps1`、`.cmd`、`.bat`。
- 登记时必须填写脚本的 Windows 绝对路径，例如：

  ```text
  D:\AI\MyService\manage.ps1
  ```

- 脚本执行时的当前工作目录固定为脚本所在目录，因此脚本可以基于当前目录定位自己的配置文件，但建议仍使用脚本目录生成明确路径。
- 管理器只向脚本传入一个固定动作参数，不传入 GPU、端口、UI 地址或其他任意参数。

## 2. 动作接口

脚本必须接受以下四个动作：

| 动作 | 用途 | 成功后的预期状态 |
|---|---|---|
| `start` | 启动服务 | `running` |
| `stop` | 停止服务 | `stopped` |
| `restart` | 重启服务 | `running` |
| `status` | 查询状态 | 返回四种规定状态之一 |

调用形式如下：

```powershell
manage.ps1 start
manage.ps1 stop
manage.ps1 restart
manage.ps1 status
```

批处理脚本的调用方式相同：

```bat
manage.cmd start
manage.cmd status
```

脚本收到未知动作时必须返回非零退出码，并输出明确错误信息。

## 3. `status` 的严格要求

`status` 必须满足以下要求：

- 只读，不启动、停止、重启或修改服务。
- 执行速度要快，默认超时时间为 3 秒。
- 成功时退出码必须为 `0`。
- 标准输出必须且只能表示以下一个小写状态：

  ```text
  running
  stopped
  unhealthy
  unknown
  ```

- 状态含义：
  - `running`：服务已运行并且可以正常使用。
  - `stopped`：服务明确未运行。
  - `unhealthy`：服务进程存在，但健康检查不通过或无法正常提供功能。
  - `unknown`：脚本无法可靠判断状态。
- 不得输出前缀、说明、JSON、日志或其他文字。例如 `status: running`、`RUNNING` 和 `服务运行中` 都不符合接口。
- 正常的行尾换行允许存在。
- `status` 返回非零退出码、执行超时或输出不符合规定时，管理器统一将状态记为 `unknown`。

管理器不会定时调用 `status`，启动管理器、打开页面、刷新页面和读取服务列表也不会执行状态脚本。只有用户点击单个服务的“深度检查”时，管理器才调用该服务一次 `status`。同一个服务的深度检查和启动、停止、重启动作会串行执行。

服务登记了本机健康检查地址后，管理器每 5 秒在自身进程内直接访问该地址，不会因此启动 PowerShell、WSL、Docker CLI 或本脚本。健康检查只验证服务可用性，`status` 仍用于用户主动要求的进程、unit 或容器级深度判断。

## 4. `start`、`stop`、`restart` 的要求

- 成功时退出码必须为 `0`。
- 失败时必须返回非零退出码，并优先把简短、明确的失败原因写入标准错误。
- 默认动作超时时间为 600 秒。
- 动作脚本不能以前台方式永久占用并一直不退出：
  - `start` 应启动后台服务，等待其达到可用状态后退出。
  - `stop` 应等待服务真正停止后退出。
  - `restart` 应完成停止和重新启动，并等待服务恢复可用后退出。
- `start` 启动的后台服务不得继续继承管理脚本的标准输出或标准错误句柄。脚本必须把后台服务输出重定向到服务自行管理的日志文件或空设备，否则后台服务可能导致管理脚本无法结束或持续占用临时输出文件。
- 管理器不在动作返回后调用 `status`。配置了健康检查地址时，动作退出后由管理器直接验证健康接口；脚本退出码为 `0` 且实际健康状态达到目标时，操作才成功。未配置健康检查时，管理器退回使用动作退出结果更新观察状态。
- 因此动作脚本仍必须在真实服务达到目标状态后才能返回成功。管理器重启后会恢复最后观察状态，并在后台通过健康接口重新确认，不会批量执行状态脚本。

## 5. 幂等要求

脚本必须能够安全地重复调用：

- 服务已经运行时再次执行 `start`，应保持运行并返回成功。
- 服务已经停止时再次执行 `stop`，应保持停止并返回成功。
- `restart` 应能处理服务当前为运行、停止或异常的情况。

场景重复切换时可能再次对目标服务调用 `start`，也可能再次对非目标服务调用 `stop`，因此不能把“已经是目标状态”当作错误。

## 6. 输出和错误信息

- 建议使用 UTF-8 输出中文错误信息。
- `status` 的标准输出只能包含规定状态，不要混入调试信息。
- 启停动作可以输出简短执行信息，但管理器不会将其作为服务运行日志保存。
- 失败原因应直接说明根本问题，例如：

  ```text
  服务进程启动后 60 秒内未监听 8000 端口
  未找到 D:\AI\MyService\config.json
  停止服务失败：进程 1234 仍在运行
  ```

- 管理器最多保留脚本标准输出和标准错误末尾的 4096 个字符用于失败摘要，因此不要输出大量日志。
- 管理器不提供 `logs` 动作，也不读取或代理服务自身日志。

## 7. PowerShell 脚本示例

以下示例只展示接口结构，服务启动和健康检查部分需要替换成真实实现：

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
            # 启动后台服务，并等待服务达到可用状态。
            Start-Service -Name $serviceName
            exit 0
        }
        "stop" {
            # 停止服务，并等待服务真正停止。
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

## 8. 批处理脚本示例

```bat
@echo off
setlocal

if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="restart" goto restart
if /I "%~1"=="status" goto status

>&2 echo 不支持的动作: %~1
exit /b 2

:start
rem 在这里启动后台服务并等待其可用。
exit /b 0

:stop
rem 在这里停止服务并等待其完全退出。
exit /b 0

:restart
call "%~f0" stop || exit /b 1
call "%~f0" start || exit /b 1
exit /b 0

:status
rem 必须根据真实进程、端口或健康接口判断，以下仅为占位示例。
echo unknown
exit /b 0
```

## 9. 登记信息与脚本的边界

以下信息由用户在管理器页面中填写，不会传给脚本：

- 服务名称
- 服务说明
- GPU 展示标签
- 服务端口
- UI 地址
- 健康检查地址
- 响应必须包含的可选文本

名称、说明、GPU、端口和 UI 地址用于展示或打开链接。健康检查地址必须是本机 `127.0.0.1`、`localhost` 或 `::1` 的完整 HTTP/HTTPS 地址；管理器以 HTTP 2xx 和可选响应文本匹配判断实际运行状态。共用同一端口的服务应使用不同路径或响应匹配文本区分身份。

脚本负责真实服务的启动、停止、重启和深度状态判断。GPU 选择、进程管理、依赖检查和服务自身日志等具体实现仍由脚本自行完成。

## 10. 接入前检查清单

在登记脚本前，建议在普通 PowerShell 或命令提示符中逐项测试：

1. 使用绝对路径执行 `status`，确认 3 秒内只输出一个规定状态。
2. 执行 `start`，确认脚本能够退出，随后 `status` 输出 `running`。
3. 再次执行 `start`，确认重复启动不会失败。
4. 执行 `restart`，确认服务恢复后 `status` 输出 `running`。
5. 执行 `stop`，确认脚本能够退出，随后 `status` 输出 `stopped`。
6. 再次执行 `stop`，确认重复停止不会失败。
7. 人为制造一次启动失败，确认脚本返回非零退出码并提供明确错误信息。
8. 登记健康检查地址；若端口可能与其他服务共用，同时填写能唯一识别本服务的响应文本。
9. 在管理器外停止和启动服务，确认界面能在两个检查周期内自动更新实际状态，且不会出现 PowerShell、WSL 或 Docker 状态轮询进程。
