[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) {
    if ((Get-ChildItem -LiteralPath $target -Force | Select-Object -First 1)) {
        throw "发布目录必须不存在或为空: $target"
    }
} else {
    New-Item -ItemType Directory -Path $target | Out-Null
}

$files = @(
    "app.js", "i18n.js", "index.html", "styles.css", "request-guard.js",
    "README.md", "README.en.md", "脚本要求.md", "SCRIPT_REQUIREMENTS.en.md",
    "requirements.txt", "Start-Manager.ps1", "Start-Manager.cmd", "Install-ManagerTask.ps1",
    "Uninstall-ManagerTask.ps1", "Build-Release.ps1", ".gitignore",
    "config\settings.example.json"
)
$files += Get-ChildItem -LiteralPath (Join-Path $projectRoot "workstation_manager") -Filter "*.py" | ForEach-Object {
    "workstation_manager\$($_.Name)"
}
foreach ($relative in $files) {
    $source = Join-Path $projectRoot $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "发布文件缺失: $relative" }
    $destinationFile = Join-Path $target $relative
    $destinationDirectory = Split-Path -Parent $destinationFile
    if (-not (Test-Path -LiteralPath $destinationDirectory)) { New-Item -ItemType Directory -Path $destinationDirectory | Out-Null }
    Copy-Item -LiteralPath $source -Destination $destinationFile
}
Write-Host "已生成干净发布目录: $target"
Write-Host "未包含 data、logs、output 或 .playwright-cli。"
