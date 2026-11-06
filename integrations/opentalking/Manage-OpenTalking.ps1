param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$lan = '192.168.100.190'
$deployment = 'E:\work\vrphoto\opentalking-lan'
$caddy = Join-Path $deployment 'caddy.exe'
$caddyfile = Join-Path $deployment 'Caddyfile'
$turnProxy = Join-Path $deployment 'turn-proxy.cjs'
$turnLauncher = Join-Path $deployment 'Start-TurnProxy.ps1'
$cert = 'E:\work\vrphoto\lan-viewer\cert.pem'
$wslEntry = Join-Path $PSScriptRoot 'manage-opentalking-wsl.sh'
$shared = 'C:\Users\xu\Desktop\本地模型启动'
$dependencies = @(
    @{ Name = 'SenseVoiceSmall'; Url = 'http://127.0.0.1:18090/v1/models'; Marker = 'SenseVoiceSmall'; Script = Join-Path $shared '3090-SenseVoiceSmall-服务管理.ps1' },
    @{ Name = 'OpenSpline Qwen3.5-4B'; Url = 'http://127.0.0.1:17863/v1/models'; Marker = 'openspline-local-qwen'; Script = 'C:\Users\xu\Documents\Codex\2026-10-21\https-github-com-openspline-openspline\local\Manage-OpenSpline-LLM.ps1' },
    @{ Name = '3090 IndexTTS 1.5'; Url = 'http://127.0.0.1:6006/health'; Marker = '"cuda_visible_devices":"1"'; Script = Join-Path $shared '3090-IndexTTS15-vLLM-服务管理.ps1' }
)

function Invoke-WslAction([string]$Mode) {
    $linuxEntry = & wsl.exe -d Ubuntu-22.04 -u xu -- wslpath -a ($wslEntry -replace '\\', '/')
    if ($LASTEXITCODE -ne 0 -or -not $linuxEntry) { throw '无法解析 OpenTalking 的 WSL 管理脚本路径。' }
    & wsl.exe -d Ubuntu-22.04 -u xu -- bash $linuxEntry $Mode
    if ($LASTEXITCODE -ne 0) { throw "OpenTalking WSL 操作 $Mode 失败，请查看 opentalking-axis.service 日志。" }
}

function Get-Proxies([int]$TargetPort = 0) {
    $lines = & netstat.exe -ano -p tcp
    if ($LASTEXITCODE -ne 0) { throw '无法读取 Windows 监听端口。' }
    $address = [regex]::Escape($lan)
    foreach ($line in $lines) {
        if ($line -notmatch "^\s*TCP\s+${address}:(18443|18444)\s+\S+\s+LISTENING\s+(\d+)\s*$") { continue }
        $port = [int]$Matches[1]
        if ($TargetPort -and $TargetPort -ne $port) { continue }
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($Matches[2])"
        $command = $owner.CommandLine -replace '/', '\'
        $expected = if ($port -eq 18443) {
            $owner.ExecutablePath -eq $caddy -and $command -match ('(?:^|\s)--config\s+(?:"{0}"|{0})(?=\s|$)' -f [regex]::Escape($caddyfile))
        } else {
            $owner.Name -eq 'node.exe' -and $command -match ('^\s*(?:"[^"]*\\node\.exe"|(?:\S*\\)?node\.exe)\s+(?:"{0}"|{0})(?=\s|$)' -f [regex]::Escape($turnProxy))
        }
        if (-not $expected) { throw "端口 $lan/$port 的进程不属于此 OpenTalking 部署。" }
        Get-Process -Id $owner.ProcessId
    }
}

function Get-Endpoint([string]$Url, [string[]]$Options = @()) {
    $body = & curl.exe --silent --fail --noproxy '*' --max-time 1 @Options $Url
    if ($LASTEXITCODE -eq 0) { return ($body -join "") }
}

function Test-LanHealth {
    $body = Get-Endpoint "https://${lan}:18443/api/health" @('--cacert', $cert)
    return $body -like '*"default_model":"fasterliveportrait"*'
}

function Start-Dependencies {
    foreach ($dependency in $dependencies) {
        $body = Get-Endpoint $dependency.Url
        if ($body -and -not $body.Contains($dependency.Marker)) {
            throw "$($dependency.Name) 的接口已被其他模型或显卡实例占用。"
        }
        if (-not $body) {
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $dependency.Script start
            if ($LASTEXITCODE -ne 0 -or (Get-Endpoint $dependency.Url) -notlike "*$($dependency.Marker)*") {
                throw "$($dependency.Name) 未就绪。"
            }
        }
    }
}

function Stop-OpenTalking {
    $errors = @()
    try { Invoke-WslAction 'stop' } catch { $errors += $_.Exception.Message }
    foreach ($port in @(18443, 18444)) {
        try {
            foreach ($proxy in @(Get-Proxies $port)) {
                Stop-Process -Id $proxy.Id
                if (-not $proxy.WaitForExit(10000)) { throw "代理进程 $($proxy.Id) 退出超时。" }
            }
        } catch { $errors += $_.Exception.Message }
    }
    if ($errors.Count) { throw ($errors -join '；') }
}

function Start-OpenTalking {
    $initial = Invoke-WslAction 'status'
    $proxies = @(Get-Proxies)
    if ($initial -notin @('stopped','running')) { throw "OpenTalking WSL 状态为 $initial，请先检查或停止。" }
    Start-Dependencies
    $launchedCaddy = $null
    try {
        Invoke-WslAction 'start'
        $wslAddress = Invoke-WslAction 'address'
        if ($wslAddress -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw 'OpenTalking WSL 地址无效。' }
        $env:OPENTALKING_WEB_UPSTREAM = "${wslAddress}:18760"
        $env:OPENTALKING_TURN_UPSTREAM_HOST = $wslAddress
        & $turnLauncher
        if (-not ($proxies | Where-Object ProcessName -eq 'caddy')) {
            & $caddy validate --config $caddyfile --adapter caddyfile
            if ($LASTEXITCODE -ne 0) { throw 'OpenTalking HTTPS 配置校验失败。' }
            $launchedCaddy = Start-Process -FilePath $caddy -ArgumentList @('run','--config',$caddyfile,'--adapter','caddyfile') `
                -WorkingDirectory $deployment -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $deployment 'proxy.out.log') `
                -RedirectStandardError (Join-Path $deployment 'proxy.err.log')
        }
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            if (@(Get-Proxies).Count -eq 2 -and (Test-LanHealth)) { return }
            if ($launchedCaddy -and $launchedCaddy.HasExited) { break }
            Start-Sleep -Milliseconds 300
        }
        throw 'OpenTalking 的 HTTPS 或音视频中继未就绪，请检查部署日志。'
    } catch {
        $failure = $_.Exception.Message
        if ($initial -eq 'stopped') {
            try {
                if ($launchedCaddy -and -not $launchedCaddy.HasExited) { Stop-Process -InputObject $launchedCaddy }
                Stop-OpenTalking
            } catch { throw "$failure；清理失败：$($_.Exception.Message)" }
        }
        throw $failure
    }
}

$lock = $null
try {
    if ($Action -eq 'status') {
        $runningDistros = (& wsl.exe --list --running --quiet) -replace '\x00', ''
        if ($LASTEXITCODE -ne 0) { throw '无法读取运行中的 WSL 发行版。' }
        if ($runningDistros -notcontains 'Ubuntu-22.04') {
            if (@(Get-Proxies).Count) { 'unhealthy' } else { 'stopped' }
            exit 0
        }
        $state = Invoke-WslAction 'status'
        $proxyCount = @(Get-Proxies).Count
        if ($state -eq 'running' -and $proxyCount -eq 2 -and (Test-LanHealth)) { 'running' }
        elseif ($state -eq 'stopped' -and $proxyCount -eq 0) { 'stopped' }
        elseif ($state -eq 'unknown') { 'unknown' }
        else { 'unhealthy' }
        exit 0
    }
    $lock = [IO.File]::Open((Join-Path $deployment 'axis-opentalking.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if ($Action -in @('stop','restart')) { Stop-OpenTalking }
    if ($Action -in @('start','restart')) { Start-OpenTalking }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
}
