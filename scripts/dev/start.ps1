param([int]$BackendPort = 8001, [int]$FrontendPort = 5174, [switch]$OpenBrowser)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$lock = $null
$started = @()
try {
    $lock = Enter-LauncherLock
    $prior = Read-LauncherState
    if ($null -ne $prior) {
        $owned = @($prior.services | Where-Object { Test-OwnedService $_ })
        if ($owned.Count -gt 0) {
            if ($owned.Count -eq @($prior.services).Count -and $prior.backend_port -eq $BackendPort -and $prior.frontend_port -eq $FrontendPort) {
                $unhealthy = @($prior.services | Where-Object {
                    if ($_.kind -ne 'http') { return $false }
                    $listener = Get-PortOccupant ([int]$_.port)
                    return (-not (Test-HttpReady $_.url)) -or $null -eq $listener -or -not (Test-ListenerRelated $_ $listener.pid)
                })
                if ($unhealthy.Count -eq 0) { Write-Host "AI Product Catalog Agent is already running at http://127.0.0.1:$FrontendPort. No duplicate processes started."; exit 0 }
            }
            throw 'Partial, unhealthy or differently configured launcher-owned runtime found. Run stop.ps1, then start.ps1.'
        }
        Remove-LauncherState
        Write-Host 'Cleared stale runtime metadata; no process was stopped.'
    }
    $info = Assert-Prerequisites $BackendPort $FrontendPort
    Assert-PortAvailable $BackendPort 'Backend'
    Assert-PortAvailable $FrontendPort 'Frontend'
    $hasKey = -not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)
    $manifest = @(Get-ServiceManifest $BackendPort $FrontendPort $hasKey)
    $runId = [guid]::NewGuid().ToString('n')
    $logDirectory = Join-Path (Join-Path $script:RuntimeRoot 'logs') $runId
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Push-Location $script:BackendRoot
    try {
        & $script:PythonPath -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed. No service was started.' }
    } finally { Pop-Location }
    $info = Get-RuntimeInfo
    if ($info.migration_status -ne 'current') { throw 'Database is not at Alembic head after migration.' }
    foreach ($service in $manifest) {
        $record = Start-ManifestService $service $logDirectory
        $started += $record
        Write-Host "Starting $($service.name) (PID $($record.pid))..."
        if ($service.kind -eq 'http') { Wait-ServiceReady $record 25 }
        else { Start-Sleep -Milliseconds 800; Wait-ServiceReady $record 2 }
        Write-Host "OK  $($service.name)"
    }
    $skipped = @()
    if (-not $hasKey) { $skipped = @('Product extraction','Product Copy','Image enhancement') }
    $state = [pscustomobject]@{
        run_id=$runId; started_at_utc=(Get-Date).ToUniversalTime().ToString('o')
        backend_port=$BackendPort; frontend_port=$FrontendPort; database_path=$info.database_path
        openai_configured=$hasKey; services=$started; skipped=$skipped
    }
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    Write-Host "Ready: http://127.0.0.1:$FrontendPort"
    Write-Host "Logs: $logDirectory"
    if (-not $hasKey) { Write-Host 'WARN  OPENAI_API_KEY missing; AI workers skipped. Deterministic workflows remain available.' }
    else { Write-Host 'OK  OPENAI_API_KEY present (not validated; no provider request sent).' }
    Write-Host 'Commands: .\status.ps1  .\stop.ps1'
    if ($OpenBrowser) {
        try { Start-Process -FilePath "http://127.0.0.1:$FrontendPort" | Out-Null }
        catch { Write-Host 'WARN  Browser could not be opened; the application remains running.' }
    }
} catch {
    Write-Host "ERROR  $($_.Exception.Message)"
    [array]::Reverse($started)
    foreach ($record in $started) { try { Stop-OwnedService $record } catch { Write-Host "WARN  Could not stop $($record.name); inspect PID $($record.pid)." } }
    $remaining = @($started | Where-Object { Test-OwnedService $_ })
    if ($remaining.Count -gt 0) {
        [pscustomobject]@{
            run_id='partial-start'; started_at_utc=(Get-Date).ToUniversalTime().ToString('o')
            backend_port=$BackendPort; frontend_port=$FrontendPort; database_path='unavailable'
            openai_configured=$false; services=$remaining; skipped=@()
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
        Write-Host 'WARN  Partial owned state retained for stop.ps1.'
    } else { Remove-LauncherState }
    exit 1
} finally { if ($null -ne $lock) { $lock.Dispose() } }
exit 0
