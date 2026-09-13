[CmdletBinding()]
param([string]$OpenCodeRoot = (Join-Path $env:USERPROFILE ".config\opencode"))

$ErrorActionPreference = "Stop"
$integrationRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginDirectory = Join-Path $OpenCodeRoot "plugins"
$skillDirectory = Join-Path $OpenCodeRoot "skills\axis-automatic-tasks"
[System.IO.Directory]::CreateDirectory($pluginDirectory) | Out-Null
[System.IO.Directory]::CreateDirectory($skillDirectory) | Out-Null
Copy-Item -LiteralPath (Join-Path $integrationRoot "plugins\axis-automatic-tasks.ts") `
    -Destination (Join-Path $pluginDirectory "axis-automatic-tasks.ts") -Force
Copy-Item -LiteralPath (Join-Path $integrationRoot "skills\axis-automatic-tasks\SKILL.md") `
    -Destination (Join-Path $skillDirectory "SKILL.md") -Force
Write-Output "AXIS automatic task plugin and skill installed in $OpenCodeRoot"
