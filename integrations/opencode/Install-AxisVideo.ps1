$ErrorActionPreference = "Stop"

$integrationRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$openCodeRoot = Join-Path $env:USERPROFILE ".config\opencode"
$pluginDirectory = Join-Path $openCodeRoot "plugins"
$axisSkillDirectory = Join-Path $openCodeRoot "skills\axis-video"
$h3SkillDirectory = Join-Path $openCodeRoot "skills\h3-ref2v-video-pipeline"

New-Item -ItemType Directory -Force -Path $pluginDirectory, $axisSkillDirectory, $h3SkillDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $integrationRoot "plugins\axis-video.ts") `
    -Destination (Join-Path $pluginDirectory "axis-video.ts") -Force
Copy-Item -LiteralPath (Join-Path $integrationRoot "skills\axis-video\SKILL.md") `
    -Destination (Join-Path $axisSkillDirectory "SKILL.md") -Force
Get-ChildItem -LiteralPath (Join-Path $integrationRoot "skills\h3-ref2v-video-pipeline") | `
    Copy-Item -Destination $h3SkillDirectory -Recurse -Force

Write-Host "AXIS Video 插件、调度 Skill 和 H3 工作流 Skill 已安装到 OpenCode。请重启 OpenCode 以加载。"
