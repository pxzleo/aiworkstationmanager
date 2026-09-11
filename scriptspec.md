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

管理器不会定时调用 `status`，打开页面、刷新页面和读取服务列表也不会执行状态脚本。只有用户点击单个服务的“深度检查”、管理器在没有默认场景时启动、或某个生命周期动作失败时，管理器才执行 `status`。启动校准对全部服务逐个串行执行，第一轮返回 `unknown` 的服务在整轮结束后重试一次；同一个服务的状态检查和启动、停止、重启动作会串行执行。

服务登记了本机健康检查地址后，管理器每 5 秒在自身进程内直接访问该地址，不会因此启动 PowerShell、WSL、Docker CLI 或本脚本。健康检查只验证服务可用性，`status` 仍用于用户主动要求的进程、unit 或容器级深度判断；期望状态未知时，因不可达或超时返回 `unknown` 的轻量探测不会覆盖 `status` 已明确返回的 `unhealthy`。

## 4. `start`、`stop`、`restart` 的要求

- AXIS 计划任务以当前管理员用户的最高可用权限运行，因此脚本可以管理其固定负责的 Windows 服务、端口转发和后台进程。脚本必须把高权限操作限制在已登记服务的固定资源范围内；遇到未知映射、未知进程或不一致的资源归属时必须明确失败，不得覆盖、删除或结束未知对象。
- 成功时退出码必须为 `0`。
- 失败时必须返回非零退出码，并优先把简短、明确的失败原因写入标准错误。
- 默认动作超时时间为 600 秒。
- 对自有 Windows `portproxy` 的 WSL 服务，`start` 必须先读取目标发行版当前 IPv4，并只刷新该服务固定监听地址和端口的映射；不得因规则名称或监听存在就沿用重启前的旧目标，也不得覆盖形态不符合该服务固定映射的未知规则。
- 已登记转发目标正确但监听丢失时，管理器及服务公共脚本必须校验归属后仅重建该条映射一次，再验证 IP Helper 实际监听；不得直接返回成功或仅重复检查，也不得重启整个 IP Helper。未知进程、冲突监听、未知目标及查询失败必须明确失败。4090/3090 NInfer、IndexTTS、SenseVoice 与其他显式登记的 WSL 转发遵循同一规则。
- 动作脚本不能以前台方式永久占用并一直不退出：
  - `start` 应启动后台服务，等待其达到可用状态后退出。
  - `stop` 应等待服务真正停止后退出。
  - `restart` 应完成停止和重新启动，并等待服务恢复可用后退出。
- `start` 启动的后台服务不得继续继承管理脚本的标准输出或标准错误句柄。脚本必须把后台服务输出重定向到服务自行管理的日志文件或空设备，否则后台服务可能导致管理脚本无法结束或持续占用临时输出文件。
- 配置了健康检查地址时，动作退出后由管理器直接验证健康接口；脚本退出码为 `0` 且实际健康状态达到目标时，操作才成功。未配置健康检查时，管理器退回使用动作退出结果更新观察状态。动作失败时管理器额外执行一次 `status`，用明确的实际状态清除失败动作遗留的错误期望状态。
- 因此动作脚本仍必须在真实服务达到目标状态后才能返回成功。配置默认场景时，管理器启动后按场景动作协调服务；未配置默认场景时，管理器逐个执行只读 `status` 校准，并对第一轮的 `unknown` 重试一次，之后只在后台通过健康接口确认。

## 5. 幂等要求

脚本必须能够安全地重复调用：

- 服务已经运行时再次执行 `start`，应保持运行并返回成功。
- 服务已经停止时再次执行 `stop`，应保持停止并返回成功。
- `restart` 应能处理服务当前为运行、停止或异常的情况。

场景重复切换时可能再次对目标服务调用 `start`，也可能再次对属于其他场景的非目标服务调用 `stop`，因此不能把“已经是目标状态”当作错误。未加入任何场景的登记服务不参与场景切换。

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

## 9. Docker Desktop 登录启动脚本

`Start-DockerDesktop.ps1` 同时供开机后台任务和登录界面任务调用，并遵循以下约束：

- `Backend` 模式由 `Docker-Desktop-PreLogon` 以 AtStartup、S4U 运行，直接启动 Docker Desktop 后台并等待 WSL2 Engine；Engine 已可用时直接成功，检测到正在启动的 Docker 进程时只等待，禁止清理正在使用的套接字。
- `Desktop` 模式由 `Docker-Desktop-Interactive` 以 AtLogOn、Interactive、Highest 运行。若 Engine 位于 Session 0，脚本读取当前固定接入的全部 WSL 模型与小智服务状态，并结合 AXIS 数据库中的 `desired_state` 区分正在启动与正在停止的服务，只记录期望运行的服务，同时记录正在运行的容器；任一服务状态为 `unknown` 或登记状态缺失时拒绝中断后台。随后停止后台实例、清理临时套接字，在当前交互会话启动并等待 Engine，逐项恢复原运行容器，再通过既有服务管理脚本逐项恢复所记录的 WSL 服务及其端口转发；单项失败不阻止其余对象恢复，最终汇总错误并返回失败。Docker 已在当前会话时只请求显示 Dashboard。新增 WSL 服务时必须显式加入固定恢复清单，不能从可写文件或脚本内容动态执行任意路径。
- 两种模式使用 `data\workstation-manager.docker-handoff.lock` 文件锁跨任务互斥，并在取得锁后重新读取 Engine 和进程状态；AXIS 的服务与场景操作在完整生命周期持有同一锁，避免快速登录任务与场景切换交叉修改服务状态或套接字。
- 登录交接必须在停止 Session 0 前将容器 ID 与固定服务名称原子写入 `%LOCALAPPDATA%\Docker\axis-handoff.json`；只接受格式正确的容器 ID 和固定白名单服务名。容器恢复后必须由 `docker inspect` 确认为 running，服务恢复后必须由固定脚本的 `status` 确认为 running；全部复核成功后才能删除清单。任务中断或自动重试时继续该清单，恢复动作必须可重复执行。
- 检测到其他会话 Docker 进程但 Engine 在 150 秒内仍未就绪时必须失败关闭，不得在旧进程仍存在时并行启动当前会话 Docker Desktop。
- 仅隔离 `%LOCALAPPDATA%\Docker\run` 和 `%LOCALAPPDATA%\docker-secrets-engine` 中名称白名单内的临时 AF_UNIX 套接字；出现未知文件时明确失败。
- 同一次启动同时处理 Ingest 与 Secrets Engine 临时目录，避免修复一个套接字后在下一个套接字再次失败。
- 最多等待 150 秒确认 Docker Engine 可响应，成功和失败都写入 `%LOCALAPPDATA%\Docker\axis-startup.log`。
- 所有 Engine、容器枚举与容器恢复命令必须显式使用本机 `desktop-linux` context，不能受用户当前 Docker context 影响；`docker desktop stop` 仍只管理本机 Docker Desktop。
- `Start-Manager.ps1` 必须在启动 AXIS Python 进程和提交默认场景前等待本机 `desktop-linux` Engine，最多 210 秒；等待期间不得获取 Docker/AXIS 共享交接锁。这样两个 AtStartup 任务同时触发时，Docker 后台先完成启动，AXIS 才能开始场景操作；超时必须明确失败。
- AXIS 应用启动后若且仅若默认场景提交返回 `docker_handoff_busy`，必须最多等待 300 秒并每 2 秒重试；这覆盖登录任务在 Engine 短暂就绪后立即开始 Session 0 交接的窗口。其他错误不得重试或隐藏，等待超时必须返回 `docker_handoff_timeout`。
- AXIS 计划任务失败后每分钟重试，最多 3 次；`Start-Manager.ps1` 的启动阶段异常必须写入 `logs\manager-startup.log` 并保持非零退出码，不能只留下计划任务结果码。
- 不修改 Docker 镜像、容器、数据盘、WSL 发行版或 restart policy；登录交接只恢复交接前正在运行或启动中的固定依赖服务，不启动交接前已停止的服务。

`Install-DockerDesktopTask.ps1` 安装或更新上述两个固定任务，并在写入后核对触发器、登录类型和登录任务最高权限。任务不保存 Windows 密码。

## 10. 登记信息与脚本的边界

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

## 11. 接入前检查清单

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
