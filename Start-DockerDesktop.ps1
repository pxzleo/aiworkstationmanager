[CmdletBinding()]
param(
    [ValidateSet('Backend', 'Desktop')]
    [string]$Mode = 'Desktop'
)

$ErrorActionPreference = 'Stop'
$dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$dockerCli = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$dockerContext = 'desktop-linux'
$axisPython = 'C:\Users\xu\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
$axisDatabase = 'D:\AIWork\4090manager\data\workstation-manager.db'
$logPath = Join-Path $env:LOCALAPPDATA 'Docker\axis-startup.log'
$lockPath = 'D:\AIWork\4090manager\data\workstation-manager.docker-handoff.lock'
$handoffPath = Join-Path $env:LOCALAPPDATA 'Docker\axis-handoff.json'
$serviceScriptRoot = Join-Path $env:USERPROFILE 'Desktop\本地模型启动'
$dependentServices = @(
    @{ Name = 'IndexTTS 1.5 vLLM'; Script = Join-Path $serviceScriptRoot '3090-IndexTTS15-vLLM-服务管理.ps1' },
    @{ Name = '3090 NInfer'; Script = Join-Path $serviceScriptRoot '3090-NInfer-服务管理.ps1' },
    @{ Name = 'SenseVoiceSmall'; Script = Join-Path $serviceScriptRoot '3090-SenseVoiceSmall-服务管理.ps1' },
    @{ Name = '4090 NInfer'; Script = Join-Path $serviceScriptRoot '4090-NInfer-服务管理.ps1' },
    @{ Name = '4090 q27'; Script = Join-Path $serviceScriptRoot '4090-q27-服务管理.ps1' },
    @{ Name = '4090 vLLM'; Script = Join-Path $serviceScriptRoot '4090-vLLM-服务管理.ps1' },
    @{ Name = '小智管理后台'; Script = Join-Path $serviceScriptRoot '小智-管理后台-服务管理.ps1' },
    @{ Name = '小智核心服务'; Script = Join-Path $serviceScriptRoot '小智-核心服务-服务管理.ps1' },
    @{ Name = '小智网易云音乐 API'; Script = Join-Path $serviceScriptRoot '小智-网易云音乐API-服务管理.ps1' }
)
$runtimeDirectories = @(
    @{
        Path = Join-Path $env:LOCALAPPDATA 'Docker\run'
        AllowedNames = @(
            'dockerInference',
            'dockerEthernetVfkit',
            'sailor-ingest.sock',
            'userAnalyticsOtlpHttp.sock'
        )
    },
    @{
        Path = Join-Path $env:LOCALAPPDATA 'docker-secrets-engine'
        AllowedNames = @('engine.sock')
    }
)

function Write-StartupLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    "$(Get-Date -Format o) $Message" | Add-Content -LiteralPath $logPath -Encoding UTF8
}

function Get-DockerServerVersion {
    param([int]$TimeoutSeconds = 10)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $dockerCli
    $startInfo.Arguments = '--context desktop-linux info --format "{{.ServerVersion}}"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            return $null
        }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch [System.InvalidOperationException] { }
            $process.WaitForExit()
            return $null
        }
        $process.WaitForExit()
        $version = $stdout.Result.Trim()
        [void]$stderr.Result
        if ($process.ExitCode -eq 0 -and $version) {
            return $version
        }
        return $null
    }
    catch [System.ComponentModel.Win32Exception] {
        return $null
    }
    finally {
        $process.Dispose()
    }
}

function Get-DockerProcesses {
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -eq 'Docker Desktop' -or $_.ProcessName -like 'com.docker.*'
    })
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds = 150)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        $remaining = ($deadline - (Get-Date)).TotalSeconds
        if ($remaining -le 0) {
            return $null
        }
        $probeTimeout = [Math]::Max(1, [Math]::Min(10, [Math]::Ceiling($remaining)))
        $version = Get-DockerServerVersion -TimeoutSeconds $probeTimeout
        if ($version) {
            return $version
        }
        $remainingMilliseconds = [Math]::Floor(($deadline - (Get-Date)).TotalMilliseconds)
        if ($remainingMilliseconds -le 0) {
            return $null
        }
        Start-Sleep -Milliseconds ([Math]::Min(3000, $remainingMilliseconds))
    }
}

function Enter-StartupLock {
    $deadline = (Get-Date).AddMinutes(5)
    do {
        try {
            return [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
        }
        catch [System.IO.IOException] {
            if ((Get-Date) -ge $deadline) {
                throw '等待另一个 Docker 开机或登录任务结束超时'
            }
            Start-Sleep -Seconds 2
        }
    } while ((Get-Date) -lt $deadline)
    throw '无法取得 Docker 开机与登录任务互斥锁'
}

function Get-DesiredServiceStates {
    if (-not (Test-Path -LiteralPath $axisPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $axisDatabase -PathType Leaf)) {
        throw '找不到 AXIS Python 或服务登记数据库，无法安全判断正在启动或停止的服务'
    }
    # Windows PowerShell 5.1 会重写多行 native -c 参数中的双引号；保持单行且只用单引号。
    $query = "import json,sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); p=sys.argv[2:]; rows=c.execute('select script_path, desired_state from registered_services where script_path in ('+','.join('?' for _ in p)+')',p).fetchall(); print(json.dumps({x.lower():s for x,s in rows}))"
    $scriptPaths = @($dependentServices | ForEach-Object { [string]$_.Script })
    $output = @(& $axisPython -c $query $axisDatabase @scriptPaths 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "读取 AXIS 服务期望状态失败：$($output -join ' ')"
    }
    try {
        return (($output -join "`n") | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "AXIS 服务期望状态不是有效 JSON：$($_.Exception.Message)"
    }
}

function Get-ServicesToRestore {
    $servicesToRestore = @()
    $desiredStates = Get-DesiredServiceStates
    foreach ($service in $dependentServices) {
        if (-not (Test-Path -LiteralPath $service.Script -PathType Leaf)) {
            throw "找不到依赖服务脚本：$($service.Script)"
        }
        $output = @(& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $service.Script status 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "读取 $($service.Name) 状态失败：$($output -join ' ')"
        }
        $state = ($output -join "`n").Trim()
        if ($state -eq 'unknown') {
            throw "无法确认 $($service.Name) 状态，拒绝中断登录前 Docker 后台"
        }
        if ($state -notin @('running', 'stopped', 'unhealthy')) {
            throw "$($service.Name) 返回了无效状态：$state"
        }
        $desiredProperty = $desiredStates.PSObject.Properties[[string]$service.Script.ToLowerInvariant()]
        if ($null -eq $desiredProperty -or $desiredProperty.Value -notin @('running', 'stopped')) {
            throw "AXIS 中缺少 $($service.Name) 的有效期望状态"
        }
        if ($desiredProperty.Value -eq 'running') {
            $servicesToRestore += $service
        }
    }
    return $servicesToRestore
}

function Restore-DependentServices {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][array]$Services)

    $errors = @()
    foreach ($service in $Services) {
        $output = @(& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $service.Script start 2>&1)
        if ($LASTEXITCODE -ne 0) {
            $errors += "恢复 $($service.Name) 失败：$($output -join ' ')"
            continue
        }
        $statusOutput = @(& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $service.Script status 2>&1)
        if ($LASTEXITCODE -ne 0) {
            $errors += "复核 $($service.Name) 状态失败：$($statusOutput -join ' ')"
            continue
        }
        $state = ($statusOutput -join "`n").Trim()
        if ($state -ne 'running') {
            $errors += "恢复 $($service.Name) 后状态不是 running：$state"
            continue
        }
        Write-StartupLog "已恢复登录前运行的服务：$($service.Name)"
    }
    return $errors
}

function Save-PendingHandoff {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Containers,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][array]$Services
    )

    $temporaryPath = "$handoffPath.tmp-$PID"
    [ordered]@{
        Version = 1
        CreatedAt = (Get-Date -Format o)
        Containers = @($Containers)
        Services = @($Services | ForEach-Object { [string]$_.Name })
    } | ConvertTo-Json | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $handoffPath -Force
}

function Get-PendingHandoff {
    if (-not (Test-Path -LiteralPath $handoffPath -PathType Leaf)) {
        return $null
    }
    try {
        $pending = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "Docker 交接恢复清单损坏，拒绝继续：$($_.Exception.Message)"
    }
    if ($pending.Version -ne 1) {
        throw "Docker 交接恢复清单版本不受支持：$($pending.Version)"
    }
    $allowedServices = @{}
    foreach ($service in $dependentServices) { $allowedServices[[string]$service.Name] = $service }
    $services = @()
    foreach ($name in @($pending.Services)) {
        if (-not $allowedServices.ContainsKey([string]$name)) {
            throw "Docker 交接恢复清单包含未知服务：$name"
        }
        $services += $allowedServices[[string]$name]
    }
    $containers = @()
    foreach ($container in @($pending.Containers)) {
        if ([string]$container -notmatch '^[0-9a-f]{12,64}$') {
            throw "Docker 交接恢复清单包含无效容器 ID：$container"
        }
        $containers += [string]$container
    }
    return @{ Containers = $containers; Services = $services }
}

function Restore-PendingHandoff {
    param([Parameter(Mandatory = $true)][hashtable]$Pending)

    $errors = @()
    foreach ($container in @($Pending.Containers)) {
        $output = @(& $dockerCli --context $dockerContext start $container 2>&1)
        if ($LASTEXITCODE -ne 0) {
            $errors += "恢复容器 $container 失败：$($output -join ' ')"
            continue
        }
        $inspection = @(& $dockerCli --context $dockerContext inspect --format '{{.State.Running}}' $container 2>&1)
        if ($LASTEXITCODE -ne 0 -or ($inspection -join "`n").Trim() -ne 'true') {
            $errors += "恢复容器 $container 后未确认处于 running：$($inspection -join ' ')"
        }
    }
    if (@($Pending.Services).Count -gt 0) {
        $errors += @(Restore-DependentServices -Services @($Pending.Services))
    }
    if ($errors.Count -gt 0) {
        throw ($errors -join '；')
    }
    Remove-Item -LiteralPath $handoffPath -Force
}

function Reset-DockerRuntimeDirectories {
    foreach ($directory in $runtimeDirectories) {
        Reset-TransientDirectory -Path $directory.Path -AllowedNames $directory.AllowedNames
    }
}

function Reset-TransientDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$AllowedNames
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
        return
    }

    $items = @(Get-ChildItem -LiteralPath $Path -Force)
    if ($items.Count -eq 0) {
        return
    }
    $unexpected = @($items | Where-Object { $AllowedNames -notcontains $_.Name })
    if ($unexpected.Count -gt 0) {
        throw "Docker 临时目录包含未知项目，拒绝自动隔离：$Path -> $($unexpected.Name -join ', ')"
    }

    $destination = "$Path.stale-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    if (Test-Path -LiteralPath $destination) {
        throw "Docker 临时目录隔离目标已存在：$destination"
    }
    Move-Item -LiteralPath $Path -Destination $destination
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
    elseif (@(Get-ChildItem -LiteralPath $Path -Force).Count -gt 0) {
        throw "隔离后 Docker 临时目录被重新写入：$Path"
    }
    Write-StartupLog "已隔离上次退出遗留的临时套接字目录：$Path -> $destination"
}

if ($env:AXIS_DOCKER_STARTUP_IMPORT_ONLY -eq '1') {
    return
}

try {
    $startupLock = Enter-StartupLock
    if (-not (Test-Path -LiteralPath $dockerDesktop) -or -not (Test-Path -LiteralPath $dockerCli)) {
        throw 'Docker Desktop 程序或命令行工具不存在'
    }

    $serverVersion = Get-DockerServerVersion
    $currentSessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    $dockerProcesses = Get-DockerProcesses

    if ($Mode -eq 'Desktop') {
        $pendingHandoff = Get-PendingHandoff
        $currentSessionProcesses = @($dockerProcesses | Where-Object SessionId -eq $currentSessionId)
        $otherSessionProcesses = @($dockerProcesses | Where-Object SessionId -ne $currentSessionId)

        if (-not $serverVersion -and $otherSessionProcesses.Count -gt 0) {
            Write-StartupLog '登录时检测到其他会话的 Docker 后台仍在初始化，等待 Engine'
            $serverVersion = Wait-DockerReady
            if (-not $serverVersion) {
                throw '其他会话的 Docker 后台在 150 秒内未就绪，拒绝启动并行桌面实例'
            }
        }
        if ($serverVersion -and $currentSessionProcesses.Count -gt 0 -and $null -eq $pendingHandoff) {
            Start-Process -FilePath $dockerDesktop
            Write-StartupLog "Docker Desktop 已在当前桌面会话运行，已请求显示界面，Engine $serverVersion"
            exit 0
        }

        $runningContainers = @()
        $servicesToRestore = @()
        if ($null -ne $pendingHandoff) {
            $runningContainers = @($pendingHandoff.Containers)
            $servicesToRestore = @($pendingHandoff.Services)
            Write-StartupLog "继续上次未完成的 Docker 会话交接，需恢复容器 $($runningContainers.Count) 个、依赖服务 $($servicesToRestore.Count) 个"
        }
        elseif ($serverVersion -and $otherSessionProcesses.Count -gt 0) {
            $servicesToRestore = @(Get-ServicesToRestore)
            $runningContainers = @(& $dockerCli --context $dockerContext ps --quiet 2>$null | Where-Object { $_ })
            if ($LASTEXITCODE -ne 0) {
                throw '读取登录前运行容器失败，拒绝执行会话交接'
            }
            Save-PendingHandoff -Containers $runningContainers -Services $servicesToRestore
            $pendingHandoff = @{ Containers = $runningContainers; Services = $servicesToRestore }
            Write-StartupLog "开始从 Session 0 交接到登录桌面，需恢复容器 $($runningContainers.Count) 个、依赖服务 $($servicesToRestore.Count) 个"
        }
        if ($serverVersion -and $otherSessionProcesses.Count -gt 0) {
            & $dockerCli desktop stop --timeout 120
            if ($LASTEXITCODE -ne 0) {
                throw "停止 Session 0 Docker Desktop 失败，退出码 $LASTEXITCODE"
            }
            $stopDeadline = (Get-Date).AddSeconds(30)
            while ((Get-DockerProcesses).Count -gt 0 -and (Get-Date) -lt $stopDeadline) {
                Start-Sleep -Seconds 1
            }
            if ((Get-DockerProcesses).Count -gt 0) {
                throw 'Session 0 Docker Desktop 停止后仍有残留进程'
            }
        }

        if ((Get-DockerProcesses).Count -eq 0) {
            Reset-DockerRuntimeDirectories
        }
        Start-Process -FilePath $dockerDesktop
        $serverVersion = Wait-DockerReady
        if (-not $serverVersion) {
            throw '登录会话 Docker Desktop 在 150 秒内未就绪'
        }
        if ($null -ne $pendingHandoff) {
            Restore-PendingHandoff -Pending $pendingHandoff
        }
        $desktopProcesses = @(Get-DockerProcesses | Where-Object SessionId -eq $currentSessionId)
        if ($desktopProcesses.Count -eq 0) {
            throw 'Docker Engine 已就绪，但当前登录会话没有 Docker Desktop 界面进程'
        }
        Write-StartupLog "Docker Desktop 已交接到登录桌面，Engine $serverVersion，恢复容器 $($runningContainers.Count) 个、依赖服务 $($servicesToRestore.Count) 个"
        exit 0
    }

    if ($serverVersion) {
        Write-StartupLog "Docker Desktop 后台已就绪，跳过重复启动，Engine $serverVersion"
        exit 0
    }

    if ($dockerProcesses.Count -eq 0) {
        Reset-DockerRuntimeDirectories
        Start-Process -FilePath $dockerDesktop
        Write-StartupLog '已在开机 S4U 会话启动 Docker Desktop 后台'
    }
    else {
        Write-StartupLog '检测到 Docker 后台进程，等待现有启动完成'
    }

    $serverVersion = Wait-DockerReady
    if (-not $serverVersion) {
        throw 'Docker Desktop 在 150 秒内未就绪，请检查 Docker 日志'
    }
    Write-StartupLog "Docker Desktop 已就绪，Engine $serverVersion"
    exit 0
}
catch {
    Write-StartupLog "启动失败：$($_.Exception.Message)"
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $startupLock) {
        $startupLock.Dispose()
    }
}
