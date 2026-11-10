param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$deployment = 'E:\LLM\Fun-ASR\lan'
$caddy = Join-Path $deployment 'caddy.exe'
$config = Join-Path $deployment 'Caddyfile'
$unit = 'fun-asr.service'
$ports = @('192.168.100.190:10095', '192.168.100.190:10443', '127.0.0.1:10443')

function Test-WslRunning {
    $distros = (& wsl.exe --list --running --quiet) -replace '\x00', ''
    if ($LASTEXITCODE -ne 0) { throw '无法查询运行中的 WSL 发行版。' }
    return $distros -contains 'Ubuntu-22.04'
}

function Invoke-Systemctl([string[]]$Arguments) {
    & wsl.exe -d Ubuntu-22.04 -u xu -- systemctl --user @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Fun-ASR systemd 操作失败：$($Arguments -join ' ')。" }
}

function Get-UnitState {
    $properties = @(Invoke-Systemctl @('show', $unit, '-p', 'LoadState', '-p', 'ExecStart', '-p', 'ActiveState'))
    if ($properties -notcontains 'LoadState=loaded' -or
        -not ($properties -match '^ExecStart=.* /home/xu/work/Fun-ASR/serve_local\.py ')) {
        throw 'WSL 用户服务与 Fun-ASR 部署不匹配。'
    }
    $state = $properties -match '^ActiveState='
    if (@($state).Count -ne 1) { throw '无法识别 Fun-ASR systemd 状态。' }
    return $state.Substring('ActiveState='.Length)
}

function Get-Proxy {
    $lines = & netstat.exe -ano -p tcp
    if ($LASTEXITCODE -ne 0) { throw '无法查询 Windows 监听端口。' }
    $listeners = foreach ($line in $lines) {
        if ($line -match '^\s*TCP\s+(\S+)\s+\S+\s+LISTENING\s+(\d+)\s*$' -and $ports -contains $Matches[1]) {
            [int]$Matches[2]
        }
    }
    $ids = @($listeners | Select-Object -Unique)
    if (!$ids.Count) { return @{ Count = 0; Process = $null } }
    if ($ids.Count -ne 1) { throw 'Fun-ASR 端口由不同的 Windows 进程占用。' }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($ids[0])"
    if (!$owner.ExecutablePath -or !$owner.CommandLine) { throw '无法验证 Fun-ASR Windows 代理进程归属，请通过 AXIS 执行。' }
    $command = $owner.CommandLine -replace '/', '\'
    $configArg = '(?:^|\s)--config\s+(?:"{0}"|{0})(?=\s|$)' -f [regex]::Escape($config)
    if ($owner.ExecutablePath -ne $caddy -or $command -notmatch $configArg) {
        throw 'Fun-ASR 端口由其他 Windows 进程占用。'
    }
    return @{ Count = @($listeners).Count; Process = (Get-Process -Id $ids[0]) }
}

function Test-Health {
    $body = & curl.exe --silent --fail --noproxy '*' --max-time 1 'http://127.0.0.1:10443/'
    return $LASTEXITCODE -eq 0 -and ($body -join '').Contains('<title>Fun-ASR-Nano')
}

function Stop-Proxy([Diagnostics.Process]$Process) {
    if ($Process -and !$Process.HasExited) {
        Stop-Process -InputObject $Process
        if (!$Process.WaitForExit(10000)) { throw 'Fun-ASR Windows 代理退出超时。' }
    }
}

function Stop-FunAsr {
    $proxy = Get-Proxy
    if (Test-WslRunning) {
        $null = Get-UnitState
        Invoke-Systemctl @('stop', $unit)
        if ((Get-UnitState) -ne 'inactive') { throw 'Fun-ASR WSL 服务未完全停止。' }
    }
    Stop-Proxy $proxy.Process
}

function Start-FunAsr {
    $proxy = Get-Proxy
    $initial = Get-UnitState
    if ($proxy.Count -eq $ports.Count -and $initial -eq 'active' -and (Test-Health)) { return }
    if ($proxy.Count -and $proxy.Count -ne $ports.Count) { throw 'Fun-ASR 代理端口不完整，请重启服务。' }

    $launched = $null
    try {
        if (!$proxy.Process) {
            & $caddy validate --config $config --adapter caddyfile
            if ($LASTEXITCODE -ne 0) { throw 'Fun-ASR Caddy 配置校验失败。' }
            $launched = Start-Process -FilePath $caddy -ArgumentList @('run', '--config', $config, '--adapter', 'caddyfile') `
                -WorkingDirectory $deployment -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $deployment 'caddy.stdout.log') `
                -RedirectStandardError (Join-Path $deployment 'caddy.stderr.log')
        }
        Invoke-Systemctl @('start', $unit)
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($timer.Elapsed.TotalSeconds -lt 240) {
            if ($launched -and $launched.HasExited) { throw 'Fun-ASR 代理启动失败，请查看 caddy.stderr.log。' }
            if ((Test-Health) -and (Get-Proxy).Count -eq $ports.Count) { return }
            Start-Sleep -Seconds 2
        }
        throw 'Fun-ASR 启动后 240 秒未就绪，请查看 fun-asr.service 和 Caddy 日志。'
    } catch {
        $failure = $_.Exception.Message
        try {
            Stop-Proxy $launched
            if ($initial -in @('inactive', 'failed')) { Invoke-Systemctl @('stop', $unit) }
        } catch { throw "$failure 清理失败：$($_.Exception.Message)" }
        throw $failure
    }
}

$lock = $null
try {
    if ($Action -eq 'status') {
        $proxy = Get-Proxy
        $state = if (Test-WslRunning) { Get-UnitState } else { 'inactive' }
        if ($state -eq 'active' -and $proxy.Count -eq $ports.Count -and (Test-Health)) { 'running' }
        elseif ($state -eq 'inactive' -and !$proxy.Count) { 'stopped' }
        else { 'unhealthy' }
        exit 0
    }

    $lock = [IO.File]::Open((Join-Path $deployment 'axis-fun-asr.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if ($Action -in @('stop', 'restart')) { Stop-FunAsr }
    if ($Action -in @('start', 'restart')) { Start-FunAsr }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
}
