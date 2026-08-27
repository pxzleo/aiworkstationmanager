[CmdletBinding()]
param(
    [ValidateSet("Logon", "Startup")]
    [string]$Trigger = "Logon",
    [string]$ConfigFile
)

$ErrorActionPreference = "Stop"
$taskName = "AXIS-AI-Workstation-Manager"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $projectRoot "Start-Manager.ps1"
$python = Get-Command python -ErrorAction Stop
$arguments = @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $startScript), "-PythonPath", ('"{0}"' -f $python.Source))
if ($ConfigFile) {
    $resolvedConfig = (Resolve-Path -LiteralPath $ConfigFile -ErrorAction Stop).Path
    $arguments += @("-ConfigFile", ('"{0}"' -f $resolvedConfig))
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($arguments -join " ") -WorkingDirectory $projectRoot
if ($Trigger -eq "Startup") {
    $taskTrigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType S4U -RunLevel Limited
} else {
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$task = New-ScheduledTask -Action $action -Trigger $taskTrigger -Principal $principal -Settings $settings -Description "AXIS AI 工作站管理器；运行日志位于项目 logs\manager.log"
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force -ErrorAction Stop | Out-Null
Write-Host "已安装当前用户计划任务: $taskName ($Trigger)。未保存账户密码。"
Write-Host "管理器日志: $(Join-Path $projectRoot 'logs\manager.log')"
