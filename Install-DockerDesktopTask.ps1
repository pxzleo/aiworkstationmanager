[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$backendTaskName = 'Docker-Desktop-PreLogon'
$desktopTaskName = 'Docker-Desktop-Interactive'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $projectRoot 'Start-DockerDesktop.ps1'
if (-not (Test-Path -LiteralPath $startScript)) {
    throw "找不到 Docker Desktop 启动脚本：$startScript"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$backendArguments = "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`" -Mode Backend"
$desktopArguments = "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`" -Mode Desktop"
$backendAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $backendArguments -WorkingDirectory $projectRoot
$desktopAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $desktopArguments -WorkingDirectory $projectRoot
$backendTrigger = New-ScheduledTaskTrigger -AtStartup
$desktopTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$backendPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType S4U -RunLevel Highest
$desktopPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::FromMinutes(15)) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1))

$backendTask = New-ScheduledTask -Action $backendAction -Trigger $backendTrigger -Principal $backendPrincipal -Settings $settings -Description 'Windows 开机且用户未登录时启动 Docker Desktop WSL2 后端'
$desktopTask = New-ScheduledTask -Action $desktopAction -Trigger $desktopTrigger -Principal $desktopPrincipal -Settings $settings -Description '用户登录 Windows 后显示 Docker Desktop 界面与托盘图标'
Register-ScheduledTask -TaskName $backendTaskName -InputObject $backendTask -Force -ErrorAction Stop | Out-Null
Register-ScheduledTask -TaskName $desktopTaskName -InputObject $desktopTask -Force -ErrorAction Stop | Out-Null

$backend = Get-ScheduledTask -TaskName $backendTaskName -ErrorAction Stop
$desktop = Get-ScheduledTask -TaskName $desktopTaskName -ErrorAction Stop
if ($backend.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskBootTrigger' -or
    $backend.Principal.LogonType.ToString() -ne 'S4U' -or
    $desktop.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -or
    $desktop.Principal.LogonType.ToString() -ne 'Interactive' -or
    $desktop.Principal.RunLevel.ToString() -ne 'Highest') {
    throw 'Docker Desktop 的后台或桌面计划任务验证失败'
}
Write-Host "已安装 Docker Desktop 开机后台任务：$backendTaskName"
Write-Host "已安装 Docker Desktop 登录界面任务：$desktopTaskName"
