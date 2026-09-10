$ErrorActionPreference = 'Stop'
$env:AXIS_DOCKER_STARTUP_IMPORT_ONLY = '1'
try {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'Start-DockerDesktop.ps1')
}
finally {
    Remove-Item Env:AXIS_DOCKER_STARTUP_IMPORT_ONLY -ErrorAction SilentlyContinue
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) "axis-docker-test-$PID"
[System.IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
try {
    $slowDocker = Join-Path $temporaryRoot 'slow-docker.exe'
    Add-Type -TypeDefinition @'
using System;
using System.Threading;
public static class SlowDocker {
    public static void Main() { Thread.Sleep(5000); Console.WriteLine("unexpected"); }
}
'@ -OutputAssembly $slowDocker -OutputType ConsoleApplication
    $dockerCli = $slowDocker
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $probeResult = Get-DockerServerVersion -TimeoutSeconds 1
    $stopwatch.Stop()
    Assert-True ($null -eq $probeResult) '单次 Docker 探针超时必须返回空结果'
    Assert-True ($stopwatch.Elapsed.TotalSeconds -lt 2.5) '单次 Docker 探针必须终止卡住的进程'

    function Get-DockerServerVersion { param([int]$TimeoutSeconds) return $null }
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $result = Wait-DockerReady -TimeoutSeconds 1
    $stopwatch.Stop()
    Assert-True ($null -eq $result) '超时探针必须返回空结果'
    Assert-True ($stopwatch.Elapsed.TotalSeconds -lt 1.5) '等待必须受总墙钟期限约束'

    $serviceScript = Join-Path $temporaryRoot 'service.ps1'
    @'
param([string]$Action)
if ($Action -eq 'start') { exit 0 }
if ($Action -eq 'status') { Write-Output 'unexpected'; Write-Output 'running'; exit 0 }
exit 1
'@ | Set-Content -LiteralPath $serviceScript -Encoding UTF8
    $serviceErrors = @(Restore-DependentServices -Services @(@{ Name = '测试服务'; Script = $serviceScript }))
    Assert-True ($serviceErrors.Count -eq 1) '多行 status 输出必须被拒绝'

    $fakeDocker = Join-Path $temporaryRoot 'docker.cmd'
    @'
@echo off
if "%3"=="start" exit /b 0
if "%3"=="inspect" (
  if "%FAKE_DOCKER_RUNNING%"=="1" (echo true) else (echo false)
  exit /b 0
)
exit /b 1
'@ | Set-Content -LiteralPath $fakeDocker -Encoding ASCII
    $dockerCli = $fakeDocker
    $handoffPath = Join-Path $temporaryRoot 'axis-handoff.json'
    Set-Content -LiteralPath $handoffPath -Value '{}' -Encoding UTF8
    $env:FAKE_DOCKER_RUNNING = '0'
    $failed = $false
    try {
        Restore-PendingHandoff -Pending @{ Containers = @('aaaaaaaaaaaa'); Services = @() }
    }
    catch {
        $failed = $true
    }
    Assert-True $failed '未验证为 running 的容器必须使恢复失败'
    Assert-True (Test-Path -LiteralPath $handoffPath) '恢复失败时必须保留 manifest'

    $env:FAKE_DOCKER_RUNNING = '1'
    Restore-PendingHandoff -Pending @{ Containers = @('aaaaaaaaaaaa'); Services = @() }
    Assert-True (-not (Test-Path -LiteralPath $handoffPath)) '全部验证成功后必须删除 manifest'
}
finally {
    Remove-Item Env:FAKE_DOCKER_RUNNING -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
