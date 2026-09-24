param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Launcher = "fl2va_int8_24gb"

$distro = "Ubuntu-22.04"
$unit = "x-minimaxh3-root.service"
$projectPath = "/home/xu/h3serve"
$startScript = "$projectPath/run.sh"
$servicePort = 8090
$gpuUuid = "GPU-24e90667-f02e-1e21-e5fa-b4bd6566ce63"
$healthUri = "http://127.0.0.1:$servicePort/"
$healthMarker = "X-MinimaxH3"
$startTimeoutSeconds = 120
$stopTimeoutSeconds = 45
$engineLoadTimeoutSeconds = 450
$sharedManagementScript = "C:\Users\xu\Desktop\本地模型启动\_服务管理公共.ps1"

function Sync-H3PortProxy {
    if (-not (Test-Path -LiteralPath $sharedManagementScript -PathType Leaf)) {
        throw "未找到 AXIS WSL 端口转发公共脚本：$sharedManagementScript"
    }
    . $sharedManagementScript
    Sync-WslPortProxy -Distro $distro -Port $servicePort
}

function Invoke-WslCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & wsl.exe -d $distro -u root -- @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    $text = (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
    if (-not $AllowFailure -and $exitCode -ne 0) {
        if (-not $text) {
            $text = "WSL 命令退出代码 $exitCode"
        }
        throw $text
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = $text }
}

function Get-UnitProperties {
    $result = Invoke-WslCommand -Arguments @(
        "systemctl", "show", $unit, "--no-pager",
        "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath,WorkingDirectory,ExecStart"
    ) -AllowFailure
    if ($result.ExitCode -ne 0) {
        return $null
    }

    $properties = @{}
    foreach ($line in ($result.Output -split "\r?\n")) {
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $properties[$parts[0]] = $parts[1]
        }
    }
    return $properties
}

function Test-OrphanOrConflict {
    $probe = Invoke-WslCommand -Arguments @(
        "bash", "-lc",
        "pgrep -af '^$projectPath/runtime/venv/bin/python $projectPath/server.py( |$)' >/dev/null || ss -H -ltn 'sport = :$servicePort' | grep -q ."
    ) -AllowFailure
    if ($probe.ExitCode -eq 0) {
        return $true
    }
    if ($probe.ExitCode -eq 1) {
        return $false
    }
    throw "无法检查 WSL 中的 H3Serve 进程和端口"
}

function Test-Health {
    try {
        $response = Invoke-WebRequest -Uri $healthUri -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300 -and
            ([string]$response.Content).Contains($healthMarker)
    }
    catch {
        return $false
    }
}

function Test-OwnedUnitProperties {
    param([Parameter(Mandatory = $true)][hashtable]$Properties)

    $escapedStartScript = [regex]::Escape($startScript)
    $exactExecStart = "^\{\s*path=$escapedStartScript\s*;\s*argv\[\]=$escapedStartScript\s*;"
    return $Properties["LoadState"] -eq "loaded" -and
        $Properties["FragmentPath"] -eq "/run/systemd/transient/$unit" -and
        $Properties["WorkingDirectory"] -eq $projectPath -and
        $Properties["ExecStart"] -match $exactExecStart
}

function Get-H3ServeProcessStatus {
    $properties = Get-UnitProperties
    if ($null -eq $properties -or $properties["LoadState"] -eq "not-found") {
        return $(if (Test-OrphanOrConflict) { "unhealthy" } else { "stopped" })
    }
    if (-not (Test-OwnedUnitProperties -Properties $properties)) {
        return "unknown"
    }
    if ($properties["ActiveState"] -eq "inactive" -and $properties["SubState"] -eq "dead") {
        return $(if (Test-OrphanOrConflict) { "unhealthy" } else { "stopped" })
    }
    if ($properties["ActiveState"] -ne "active" -or $properties["SubState"] -ne "running") {
        return "unhealthy"
    }

    $mainPid = 0
    if (-not [int]::TryParse($properties["MainPID"], [ref]$mainPid) -or $mainPid -le 0) {
        return "unhealthy"
    }
    return $(if (Test-Health) { "running" } else { "unhealthy" })
}

function Get-EngineSnapshot {
    return Invoke-RestMethod -Uri "http://127.0.0.1:$servicePort/healthz" `
        -Method Get -TimeoutSec 2
}

function Get-H3ServeStatus {
    $processState = Get-H3ServeProcessStatus
    if ($processState -ne "running") {
        return $processState
    }

    try {
        $snapshot = Get-EngineSnapshot
    }
    catch {
        return "unhealthy"
    }
    # AXIS tracks the shared service; generation readiness belongs to the Skill.
    return $(if ($snapshot.status -eq "ok") { "running" } else { "unhealthy" })
}

function Wait-ForStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Expected,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ((Get-H3ServeStatus) -eq $Expected) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "H3Serve 未在 $TimeoutSeconds 秒内达到 $Expected 状态"
}

function Start-H3ServeProcess {
    $state = Get-H3ServeProcessStatus
    if ($state -eq "running") {
        Sync-H3PortProxy
        return
    }
    if ($state -ne "stopped") {
        throw "H3Serve 当前状态为 $state，拒绝覆盖现有 unit、进程或端口"
    }

    $pathCheck = Invoke-WslCommand -Arguments @("test", "-x", $startScript) -AllowFailure
    if ($pathCheck.ExitCode -ne 0) {
        throw "未找到可执行启动脚本：$startScript"
    }
    $null = Invoke-WslCommand -Arguments @(
        "systemd-run", "--quiet", "--collect", "--unit=$unit",
        "--property=WorkingDirectory=$projectPath", "--property=Delegate=memory",
        "--setenv=CUDA_VISIBLE_DEVICES=$gpuUuid", "--", $startScript
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($startTimeoutSeconds)
    do {
        if ((Get-H3ServeProcessStatus) -eq "running") {
            Sync-H3PortProxy
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "H3Serve 服务进程未在 $startTimeoutSeconds 秒内达到 running 状态"
}

function Select-H3ServeEngine {
    $snapshot = Get-EngineSnapshot
    if ($snapshot.engine_control.switching) {
        throw "H3Serve 正在切换模型，请等待切换完成"
    }
    if (
        [string]$snapshot.active_launcher -in @("fl2va_int8_24gb", "ref2va_int8_24gb") -and
        [string]$snapshot.warm_state.status -eq "ready"
    ) {
        return
    }

    $body = @{
        launcher = $Launcher
        model_variant = "lora"
        host_memory_limit_gib = 41
    } | ConvertTo-Json -Compress
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$servicePort/api/v1/engine" `
            -Method Put -ContentType "application/json" -Body $body `
            -TimeoutSec $engineLoadTimeoutSeconds
    }
    catch {
        $details = [string]$_.Exception.Message
        if ($null -ne $_.ErrorDetails -and $_.ErrorDetails.Message) {
            $details = [string]$_.ErrorDetails.Message
        }
        throw "自动载入 $Launcher 失败：$details"
    }
    if (
        [string]$response.active_launcher -ne $Launcher -or
        [string]$response.warm_state.status -ne "ready"
    ) {
        throw "自动载入 $Launcher 后未达到 ready 状态"
    }
    Wait-ForStatus -Expected "running" -TimeoutSeconds 10
}

function Start-H3Serve {
    Start-H3ServeProcess
    Select-H3ServeEngine
    Sync-H3PortProxy
}

function Stop-H3Serve {
    $processState = Get-H3ServeProcessStatus
    if ($processState -eq "stopped") {
        return
    }
    if ($processState -ne "running") {
        throw "H3Serve 进程状态为 $processState，无法安全确认模型身份，拒绝停止"
    }

    try {
        $snapshot = Get-EngineSnapshot
    }
    catch {
        throw "无法读取 H3Serve 模型身份，拒绝停止共享服务：$($_.Exception.Message)"
    }
    if ($snapshot.engine_control.switching) {
        throw "H3Serve 正在切换模型，拒绝停止共享服务"
    }

    $properties = Get-UnitProperties
    if ($null -eq $properties -or -not (Test-OwnedUnitProperties -Properties $properties)) {
        throw "检测到不属于 $unit 的 H3Serve 进程或端口，拒绝停止"
    }

    try {
        $finalSnapshot = Get-EngineSnapshot
    }
    catch {
        throw "停止前无法再次确认 H3Serve 模型身份，拒绝停止共享服务：$($_.Exception.Message)"
    }
    if ($finalSnapshot.engine_control.switching) {
        throw "停止前 H3Serve 开始切换模型，拒绝停止共享服务"
    }
    $null = Invoke-WslCommand -Arguments @("systemctl", "stop", $unit)
    $deadline = [DateTime]::UtcNow.AddSeconds($stopTimeoutSeconds)
    do {
        if ((Get-H3ServeProcessStatus) -eq "stopped") {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "H3Serve 服务进程未在 $stopTimeoutSeconds 秒内停止"
}

try {
    switch ($Action) {
        "start" { Start-H3Serve }
        "stop" { Stop-H3Serve }
        "restart" {
            Stop-H3Serve
            Start-H3Serve
        }
        "status" { [Console]::Out.WriteLine((Get-H3ServeStatus)) }
    }
    exit 0
}
catch {
    if ($Action -eq "status") {
        [Console]::Out.WriteLine("unknown")
        exit 0
    }
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
