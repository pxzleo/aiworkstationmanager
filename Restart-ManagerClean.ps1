[CmdletBinding()]
param([switch]$CheckOnly, [switch]$Pause)

$ErrorActionPreference = 'Stop'
$taskName = 'AXIS-AI-Workstation-Manager'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $projectRoot 'Start-Manager.ps1'

function Get-AxisProcessFamily {
    param([object[]]$Processes, [int[]]$PortOwners, [string]$PythonPath)

    $byId = @{}
    foreach ($process in $Processes) { $byId[[int]$process.ProcessId] = $process }
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($owner in $PortOwners) {
        $process = $byId[$owner]
        if ($null -eq $process -or $process.Name -ne 'python.exe' -or
            $process.CommandLine -notmatch '(?i)(?:^|\s)-m\s+workstation_manager(?:\s|$)') {
            throw "端口由未验证的进程占用，拒绝结束 PID ${owner}。"
        }
        $current = $process
        $chain = @()
        while ($null -ne $current -and $current.Name -eq 'python.exe' -and
            $current.CommandLine -match '(?i)(?:^|\s)-m\s+workstation_manager(?:\s|$)') {
            $chain += $current
            $current = $byId[[int]$current.ParentProcessId]
        }
        $expectedPython = @($chain | Where-Object {
            $_.ExecutablePath -ieq $PythonPath -or $_.CommandLine -like "*$PythonPath*"
        }).Count -gt 0
        $taskLauncher = $null -ne $current -and $current.Name -eq 'powershell.exe' -and
            $current.CommandLine -like "*$startScript*"
        if (-not $expectedPython -and -not $taskLauncher) {
            throw "PID ${owner} 无法关联到 AXIS 计划任务使用的 Python，拒绝结束。"
        }
        foreach ($member in $chain) { [void]$ids.Add([int]$member.ProcessId) }
    }

    foreach ($process in $Processes) {
        if ($process.Name -ne 'python.exe' -or
            $process.CommandLine -notmatch '(?i)(?:^|\s)-m\s+workstation_manager(?:\s|$)') { continue }
        $parent = $byId[[int]$process.ParentProcessId]
        if ($null -ne $parent -and $parent.Name -eq 'powershell.exe' -and
            ($process.ExecutablePath -ieq $PythonPath -or $process.CommandLine -like "*$PythonPath*") -and
            $parent.CommandLine -like "*$startScript*") {
            [void]$ids.Add([int]$process.ProcessId)
        }
    }

    do {
        $countBefore = $ids.Count
        foreach ($process in $Processes) {
            if ($process.Name -eq 'python.exe' -and $ids.Contains([int]$process.ParentProcessId) -and
                $process.CommandLine -match '(?i)((?:^|\s)-m\s+workstation_manager(?:\s|$)|multiprocessing\.spawn|resource_tracker)') {
                [void]$ids.Add([int]$process.ProcessId)
            }
        }
    } while ($ids.Count -gt $countBefore)
    return @($ids | ForEach-Object { $byId[$_] })
}

function Get-StartupOperations {
    param([string]$PythonPath, [string]$DatabasePath, [string]$SinceUtc)

    $code = @'
import json, sqlite3, sys
from pathlib import Path
uri = 'file:' + Path(sys.argv[1]).as_posix() + '?mode=ro'
with sqlite3.connect(uri, uri=True, timeout=5) as db:
    active = db.execute('SELECT COUNT(*) FROM operations WHERE status IN (?,?)', ('queued', 'running')).fetchone()[0]
    tasks = db.execute('SELECT COUNT(*) FROM automatic_tasks WHERE status=?', ('running',)).fetchone()[0]
    failures = db.execute('SELECT kind, action, error_summary FROM operations WHERE requested_by=? AND source_ip=? AND created_at>=? AND status=?', ('system', 'startup', sys.argv[2], 'failed')).fetchall()
    base_required = db.execute('SELECT COUNT(*) FROM base_service_members').fetchone()[0] > 0
    scene_required = db.execute('SELECT COUNT(*) FROM scenes WHERE is_default=1').fetchone()[0] > 0
    base_success = db.execute('SELECT COUNT(*) FROM operations WHERE kind=? AND action=? AND requested_by=? AND source_ip=? AND created_at>=? AND status=?', ('service_group', 'start_base', 'system', 'startup', sys.argv[2], 'succeeded')).fetchone()[0] > 0
    scene_success = db.execute('SELECT COUNT(*) FROM operations WHERE kind=? AND action=? AND requested_by=? AND source_ip=? AND created_at>=? AND status=?', ('scene', 'activate', 'system', 'startup', sys.argv[2], 'succeeded')).fetchone()[0] > 0
print(json.dumps({'active': active, 'tasks': tasks, 'failures': failures, 'base_required': base_required, 'scene_required': scene_required, 'base_success': base_success, 'scene_success': scene_success}))
'@
    $output = & $PythonPath -c $code $DatabasePath $SinceUtc 2>&1
    if ($LASTEXITCODE -ne 0) { throw "无法读取 AXIS 操作状态：$output" }
    return $output | ConvertFrom-Json
}

try {
    $task = Get-ScheduledTask -TaskName $taskName
    if ($task.Actions.Count -ne 1 -or
        [IO.Path]::GetFileName($task.Actions[0].Execute) -ne 'powershell.exe' -or
        $task.Actions[0].WorkingDirectory -ne $projectRoot -or
        $task.Actions[0].Arguments -notlike "*$startScript*") {
        throw '计划任务动作与当前 AXIS 安装路径不符，拒绝清理进程。'
    }
    $arguments = $task.Actions[0].Arguments
    $configMatch = [regex]::Match($arguments, '-ConfigFile\s+"([^"]+)"')
    $pythonMatch = [regex]::Match($arguments, '-PythonPath\s+"([^"]+)"')
    if (-not $configMatch.Success -or -not $pythonMatch.Success) {
        throw '计划任务缺少明确的 ConfigFile 或 PythonPath。'
    }
    $settings = Get-Content -LiteralPath $configMatch.Groups[1].Value -Raw | ConvertFrom-Json
    $pythonPath = $pythonMatch.Groups[1].Value
    $databasePath = if ([IO.Path]::IsPathRooted($settings.database_path)) {
        $settings.database_path
    } else { Join-Path $projectRoot $settings.database_path }
    $ports = @([int]$settings.port, [int]$settings.file_service_port)
    if ($ports.Count -ne 2 -or $ports[0] -eq $ports[1] -or
        -not (Test-Path -LiteralPath $pythonPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
        throw 'AXIS 配置中的端口、Python 或数据库路径无效。'
    }

    $admin = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin -and -not $CheckOnly) {
        Write-Host '需要管理员权限清理 AXIS 残留进程，正在请求 Windows 授权。'
        $launcher = Join-Path $projectRoot 'Restart-ManagerClean.ps1'
        $childArguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $launcher + '"'))
        if ($Pause) { $childArguments += '-Pause' }
        $child = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Normal -Wait -PassThru `
            -ArgumentList $childArguments
        exit $child.ExitCode
    }

    $before = Get-StartupOperations $pythonPath $databasePath '9999-01-01T00:00:00+00:00'
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -in $ports })
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    $processes = @(Get-CimInstance Win32_Process)
    $family = @(Get-AxisProcessFamily $processes $owners $pythonPath)
    if ($before.tasks -gt 0 -or
        ($before.active -gt 0 -and ($task.State -ne 'Ready' -or $family.Count -gt 0))) {
        throw "AXIS 仍有 $($before.active) 个服务操作和 $($before.tasks) 个自动任务运行，拒绝中断。"
    }
    if ($before.active -gt 0) { Write-Host '发现旧的操作记录；计划任务和管理进程均已退出，启动时将由 AXIS 处理。' }
    Write-Host "计划任务：$taskName；状态：$($task.State)"
    Write-Host "AXIS 端口：$($ports -join ', ')；已验证管理进程：$(@($family.ProcessId) -join ', ')"
    if ($CheckOnly) { Write-Host '只读检查完成，未重启服务。'; return }

    $startedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName
        $stopDeadline = (Get-Date).AddSeconds(20)
        do {
            Start-Sleep -Seconds 1
            $task = Get-ScheduledTask -TaskName $taskName
        } while ($task.State -eq 'Running' -and (Get-Date) -lt $stopDeadline)
        if ($task.State -ne 'Ready') { throw "计划任务未正常停止：$($task.State)" }
    } elseif ($task.State -ne 'Ready') {
        throw "计划任务状态不允许重启：$($task.State)"
    }

    foreach ($process in ($family | Sort-Object CreationDate -Descending)) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ProcessId)"
        if ($null -eq $current) { continue }
        if ($current.Name -ne 'python.exe' -or $current.CreationDate -ne $process.CreationDate) {
            throw "PID $($process.ProcessId) 身份已变化，停止清理。"
        }
        Stop-Process -Id $process.ProcessId -Force
    }
    Start-Sleep -Seconds 2
    $remaining = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -in $ports })
    if ($remaining.Count -gt 0) {
        throw "AXIS 端口仍被占用：$(($remaining | ForEach-Object { "$($_.LocalPort)/PID $($_.OwningProcess)" }) -join ', ')"
    }

    Start-ScheduledTask -TaskName $taskName
    Write-Host '计划任务已启动，等待主接口、文件服务及启动操作完成……'
    $deadline = (Get-Date).AddMinutes(12)
    do {
        Start-Sleep -Seconds 10
        $task = Get-ScheduledTask -TaskName $taskName
        if ($task.State -ne 'Running') {
            $info = Get-ScheduledTaskInfo -TaskName $taskName
            throw "计划任务退出：$($task.State)，结果码 $($info.LastTaskResult)。"
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($ports[0])/api/v1/health" -TimeoutSec 3
            $fileHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$($ports[1])/health" -TimeoutSec 3
        } catch [System.Net.WebException] {
            Write-Host '启动中，健康接口暂未就绪。'
            continue
        }
        $startup = Get-StartupOperations $pythonPath $databasePath $startedAtUtc
        if ($startup.failures.Count -gt 0) { throw "启动操作失败：$($startup.failures | ConvertTo-Json -Compress)" }
        if ($startup.active -eq 0 -and $startup.base_required -and -not $startup.base_success) {
            throw '基础服务启动未成功完成。'
        }
        if ($startup.active -eq 0 -and $startup.scene_required -and -not $startup.scene_success) {
            throw '默认场景未执行或未成功完成；请检查 GPU 租约和 AXIS 操作记录。'
        }
        if ($health.status -eq 'healthy' -and $fileHealth.status -eq 'ok' -and
            $fileHealth.root_available -and $startup.active -eq 0 -and
            (-not $startup.base_required -or $startup.base_success) -and
            (-not $startup.scene_required -or $startup.scene_success)) {
            $newListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { $_.LocalPort -in $ports })
            if (@($newListeners | Select-Object -ExpandProperty LocalPort -Unique).Count -ne 2 -or
                @($newListeners | Select-Object -ExpandProperty OwningProcess -Unique).Count -ne 1) {
                throw '两个 AXIS 端口没有归属同一个管理进程。'
            }
            $newOwner = [int]$newListeners[0].OwningProcess
            $newProcesses = @(Get-CimInstance Win32_Process)
            $newFamily = @(Get-AxisProcessFamily $newProcesses @($newOwner) $pythonPath)
            $newManager = @($newFamily | Where-Object { $_.ProcessId -eq $newOwner })
            if ($newManager.Count -ne 1 -or
                $newManager[0].CreationDate.ToUniversalTime() -lt [DateTimeOffset]::Parse($startedAtUtc).UtcDateTime) {
                throw '监听进程不是本次启动的 AXIS 管理器。'
            }
            Write-Host "恢复成功：$($ports -join '/') 已监听，管理器 $($health.status)，文件服务 $($fileHealth.status)。" -ForegroundColor Green
            if ($Pause) { Read-Host '按回车关闭窗口' | Out-Null }
            return
        }
        Write-Host "启动中：管理器 $($health.status)，待完成操作 $($startup.active)，默认场景成功 $($startup.scene_success)。"
    } while ((Get-Date) -lt $deadline)
    throw '等待 AXIS 完全就绪超时（12 分钟）。'
} catch {
    [Console]::Error.WriteLine("AXIS 恢复失败：$($_.Exception.Message)")
    if ($Pause) { Read-Host '按回车关闭窗口' | Out-Null }
    exit 1
}
