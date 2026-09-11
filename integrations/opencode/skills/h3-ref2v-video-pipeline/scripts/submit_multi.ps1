param(
    [Parameter(Mandatory)][string[]]$ApiJson,
    [Parameter(Mandatory)][string[]]$ClientId,
    [string[]]$SubmitJson,
    [switch]$Wait,
    [int]$TimeoutMinutes = 30,
    [int]$PollSeconds = 20,
    [string]$ApiBase = 'http://127.0.0.1:8189'
)
$ErrorActionPreference = 'Stop'

if ($ApiJson.Count -ne $ClientId.Count) { throw 'ApiJson and ClientId counts differ' }
if ($SubmitJson -and $SubmitJson.Count -ne $ApiJson.Count) { throw 'SubmitJson count differs' }

$queue = Invoke-RestMethod "$ApiBase/queue"
if ($queue.queue_running.Count -or $queue.queue_pending.Count) {
    throw "ComfyUI queue is not empty: running=$($queue.queue_running.Count) pending=$($queue.queue_pending.Count)"
}

$jobs = @()
for ($i = 0; $i -lt $ApiJson.Count; $i++) {
    # 信封封装：/prompt body 必须是 {"prompt": <图>, "client_id": ...}，裸图会报 no_prompt
    $graph = Get-Content -LiteralPath $ApiJson[$i] -Raw | ConvertFrom-Json
    $body = @{ prompt = $graph; client_id = $ClientId[$i] } | ConvertTo-Json -Depth 100
    $submission = Invoke-RestMethod "$ApiBase/prompt" -Method Post -ContentType 'application/json' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
    $out = $SubmitJson[$i]
    if (-not $out) { $out = [System.IO.Path]::ChangeExtension((Resolve-Path $ApiJson[$i]).Path, '.submit.json') }
    $submission | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 -LiteralPath $out
    Write-Host "submitted: $($ClientId[$i]) prompt_id=$($submission.prompt_id)"
    $jobs += [pscustomobject]@{ client_id = $ClientId[$i]; prompt_id = $submission.prompt_id; status = 'pending'; done = $false }
}

if (-not $Wait) { return }

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
while ((Get-Date) -lt $deadline -and ($jobs | Where-Object { -not $_.done })) {
    foreach ($j in $jobs) {
        if ($j.done) { continue }
        try {
            $raw = Invoke-WebRequest "$ApiBase/history/$($j.prompt_id)" -UseBasicParsing | Select-Object -ExpandProperty Content
            $h = $raw | ConvertFrom-Json
            if ($h.PSObject.Properties.Name -contains $j.prompt_id) {
                $j.status = $h.$($j.prompt_id).status.status_str
                Write-Host "$($j.client_id): $($j.status)"
                if ($j.status -eq 'success' -or $j.status -eq 'error') { $j.done = $true }
            }
        } catch { Write-Host "poll error: $($_.Exception.Message)" }
    }
    $remaining = @($jobs | Where-Object { -not $_.done })
    if ($remaining) { Start-Sleep -Seconds $PollSeconds }
}
$pending = $jobs | Where-Object { -not $_.done }
if ($pending) { throw "TIMEOUT after ${TimeoutMinutes} min: $($pending | ForEach-Object { $_.client_id })" }
$failed = $jobs | Where-Object { $_.status -eq 'error' }
if ($failed) { throw "generation failed: $($failed | ForEach-Object { $_.client_id })" }
Write-Host 'done: all success'
