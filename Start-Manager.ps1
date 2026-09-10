[CmdletBinding()]
param(
    [string]$ConfigFile,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDirectory = Join-Path $projectRoot "logs"
try {
    [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
}
catch {
    [Console]::Error.WriteLine("无法创建管理器启动日志目录：$($_.Exception.Message)")
    exit 1
}
$startupLog = Join-Path $logDirectory "manager-startup.log"
trap {
    $originalMessage = $_.Exception.Message
    try {
        "$(Get-Date -Format o) $originalMessage" | Add-Content -LiteralPath $startupLog -Encoding UTF8 -ErrorAction Stop
    }
    catch {
        [Console]::Error.WriteLine("无法写入管理器启动日志：$($_.Exception.Message)")
    }
    [Console]::Error.WriteLine($originalMessage)
    exit 1
}
$hadConfigEnvironment = Test-Path -LiteralPath Env:WM_CONFIG_FILE
$previousConfigEnvironment = if ($hadConfigEnvironment) { $env:WM_CONFIG_FILE } else { $null }

try {
    if ($ConfigFile) {
        $resolvedConfig = (Resolve-Path -LiteralPath $ConfigFile -ErrorAction Stop).Path
        $env:WM_CONFIG_FILE = $resolvedConfig
    }

    $pythonExecutable = if ($PythonPath) { (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $pythonExecutable) {
        throw "未找到 Python。请安装 Python 3 并确保 python 位于 PATH。"
    }

    $dockerCli = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    if (-not (Test-Path -LiteralPath $dockerCli -PathType Leaf)) {
        throw "未找到 Docker CLI，AXIS 无法在默认场景启动前确认 Docker Engine。"
    }
    function Get-DockerServerVersion {
        param([int]$TimeoutSeconds)

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
            if (-not $process.Start()) { return $null }
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
            if ($process.ExitCode -eq 0 -and $version) { return $version }
            return $null
        }
        catch [System.ComponentModel.Win32Exception] {
            return $null
        }
        finally {
            $process.Dispose()
        }
    }

    $dockerDeadline = (Get-Date).AddSeconds(210)
    $dockerVersion = $null
    while (-not $dockerVersion) {
        $remaining = ($dockerDeadline - (Get-Date)).TotalSeconds
        if ($remaining -le 0) { break }
        $probeTimeout = [Math]::Max(1, [Math]::Min(10, [Math]::Ceiling($remaining)))
        $dockerVersion = Get-DockerServerVersion -TimeoutSeconds $probeTimeout
        if ($dockerVersion) { break }
        $remainingMilliseconds = [Math]::Floor(($dockerDeadline - (Get-Date)).TotalMilliseconds)
        if ($remainingMilliseconds -le 0) { break }
        Start-Sleep -Milliseconds ([Math]::Min(3000, $remainingMilliseconds))
    }
    if (-not $dockerVersion) {
        throw "本机 Docker Desktop Engine 未在 210 秒内就绪，拒绝启动 AXIS 默认场景。"
    }

    Push-Location -LiteralPath $projectRoot
    try {
        & $pythonExecutable -m workstation_manager
        if ($LASTEXITCODE -ne 0) {
            throw "管理器启动失败，Python 退出代码: $LASTEXITCODE。请检查 logs\manager.log。"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($hadConfigEnvironment) {
        $env:WM_CONFIG_FILE = $previousConfigEnvironment
    }
    else {
        if (Test-Path -LiteralPath Env:WM_CONFIG_FILE) {
            Remove-Item -LiteralPath Env:WM_CONFIG_FILE -ErrorAction Stop
        }
    }
}
