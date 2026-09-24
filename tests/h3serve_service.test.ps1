$ErrorActionPreference = 'Stop'
$path = Join-Path (Split-Path -Parent $PSScriptRoot) 'integrations/h3serve/Manage-H3Serve.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors -join '; ') }
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Invoke-Expression $definition.Extent.Text
}
function Assert-Equal($Actual, $Expected, $Message) {
    if ($Actual -ne $Expected) { throw "$Message : expected=$Expected actual=$Actual" }
}
$Launcher = 'fl2va_int8_24gb'
$servicePort = 8090
$engineLoadTimeoutSeconds = 450
$stopTimeoutSeconds = 1
$unit = 'x-minimaxh3-root.service'
$script:processState = 'running'
function Get-H3ServeProcessStatus { return $script:processState }
function Get-EngineSnapshot { return $script:snapshot }
foreach ($model in @('fl2va_int8_24gb', 'ref2va_int8_24gb', $null)) {
    foreach ($switching in @($false, $true)) {
        $script:snapshot = [pscustomobject]@{
            status = 'ok'; active_launcher = $model
            warm_state = @{ status = $(if ($model) { 'ready' } else { 'cold' }) }
            engine_control = @{ switching = $switching }
        }
        Assert-Equal (Get-H3ServeStatus) 'running' "Model changes must preserve service state ($model/$switching)"
    }
}
$script:processState = 'stopped'
Assert-Equal (Get-H3ServeStatus) 'stopped' 'Stopped process must remain stopped'
$script:processState = 'unknown'
Assert-Equal (Get-H3ServeStatus) 'unknown' 'Foreign unit must not become running'
$script:processState = 'running'
$script:snapshot = @{ status = 'error' }
Assert-Equal (Get-H3ServeStatus) 'unhealthy' 'Invalid health must fail'

$script:loads = 0
function Invoke-RestMethod {
    param($Uri, $Method, $ContentType, $Body, $TimeoutSec)
    $request = $Body | ConvertFrom-Json
    Assert-Equal $request.launcher 'fl2va_int8_24gb' 'Cold start must use FL2VA'
    Assert-Equal $request.model_variant 'lora' 'Production default must use LoRA'
    Assert-Equal $request.host_memory_limit_gib 41 'Host memory budget must be preserved'
    $script:loads++
    return @{ active_launcher = $request.launcher; warm_state = @{status = 'ready'} }
}
function Wait-ForStatus { param($Expected, $TimeoutSeconds) }
$script:snapshot = @{status = 'ok'; active_launcher = $null; warm_state = @{status = 'cold'}; engine_control = @{switching = $false}}
Select-H3ServeEngine
Assert-Equal $script:loads 1 'Cold running process must load a model'
$script:snapshot.active_launcher = 'ref2va_int8_24gb'
$script:snapshot.warm_state.status = 'ready'
Select-H3ServeEngine
Assert-Equal $script:loads 1 'Repeated start must preserve ready Ref2VA'
$script:snapshot.engine_control.switching = $true
$failed = $false
try { Select-H3ServeEngine } catch { $failed = $true }
Assert-Equal $failed $true 'Start must not race an engine switch'
$script:snapshot.engine_control.switching = $false
function Get-UnitProperties { return @{owned = $true} }
function Test-OwnedUnitProperties { param($Properties) return $Properties.owned }
$script:stops = 0
function Invoke-WslCommand {
    param($Arguments)
    Assert-Equal ($Arguments -join ' ') "systemctl stop $unit" 'Only owned H3 unit may stop'
    $script:stops++
    $script:processState = 'stopped'
}
Stop-H3Serve
Assert-Equal $script:stops 1 'Unified service must stop when Ref2VA is loaded'
Write-Output 'PASS: H3 service identity, cold start, reuse, switch guard and stop'
