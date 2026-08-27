[CmdletBinding()]
param(
    [string]$ConfigFile,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
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
