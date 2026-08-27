[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$taskName = "AXIS-AI-Workstation-Manager"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "计划任务不存在: $taskName"
    return
}
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "已删除计划任务: $taskName。现有管理器进程和日志文件未被修改。"
