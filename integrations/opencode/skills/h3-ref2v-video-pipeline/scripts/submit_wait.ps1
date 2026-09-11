param(
    [Parameter(Mandatory)][string]$ApiJson,
    [Parameter(Mandatory)][string]$ClientId,
    [Parameter(Mandatory)][string]$SubmitJson,
    [string]$HistoryJson,
    [int]$TimeoutMinutes = 25,
    [int]$PollSeconds = 20,
    [string]$ApiBase = 'http://127.0.0.1:8189'
)
$ErrorActionPreference = 'Stop'

$queue = Invoke-RestMethod "$ApiBase/queue"
if ($queue.queue_running.Count -or $queue.queue_pending.Count) {
    throw "ComfyUI queue is not empty: running=$($queue.queue_running.Count) pending=$($queue.queue_pending.Count)"
}

$graph = Get-Content -LiteralPath $ApiJson -Raw | ConvertFrom-Json
$body = @{ prompt = $graph; client_id = $ClientId } | ConvertTo-Json -Depth 100
$submission = Invoke-RestMethod "$ApiBase/prompt" -Method Post -ContentType 'application/json' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$submission | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 -LiteralPath $SubmitJson
$pid2 = $submission.prompt_id
Write-Host "prompt_id: $pid2"

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$done = $false
$status = 'pending'
while ((Get-Date) -lt $deadline -and -not $done) {
    try {
        $raw = Invoke-WebRequest "$ApiBase/history/$pid2" -UseBasicParsing | Select-Object -ExpandProperty Content
        $h = $raw | ConvertFrom-Json
        if ($h.PSObject.Properties.Name -contains $pid2) {
            $status = $h.$pid2.status.status_str
            Write-Host "status: $status"
            if ($status -eq 'success' -or $status -eq 'error') {
                $done = $true
                if ($HistoryJson) {
                    $h.$pid2 | ConvertTo-Json -Depth 50 | Set-Content -Encoding utf8 -LiteralPath $HistoryJson
                }
            }
        }
    } catch {
        Write-Host "poll error: $($_.Exception.Message)"
    }
    if (-not $done) { Start-Sleep -Seconds $PollSeconds }
}
if (-not $done) { throw "TIMEOUT after ${TimeoutMinutes} min, last status: $status" }
if ($status -eq 'error') { throw "generation failed with status error" }
Write-Host 'done: success'
