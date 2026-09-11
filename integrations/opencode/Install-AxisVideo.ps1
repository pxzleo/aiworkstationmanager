$ErrorActionPreference = "Stop"

$integrationRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$openCodeRoot = Join-Path $env:USERPROFILE ".config\opencode"
$pluginDirectory = Join-Path $openCodeRoot "plugins"
$skillDirectory = Join-Path $openCodeRoot "skills\axis-video"

New-Item -ItemType Directory -Force -Path $pluginDirectory, $skillDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $integrationRoot "plugins\axis-video.ts") `
    -Destination (Join-Path $pluginDirectory "axis-video.ts") -Force
Copy-Item -LiteralPath (Join-Path $integrationRoot "skills\axis-video\SKILL.md") `
    -Destination (Join-Path $skillDirectory "SKILL.md") -Force

Write-Host "AXIS Video 已安装到 OpenCode。请重启 OpenCode 以加载 axis_video_submit 工具。"
