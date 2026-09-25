Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$lock = $null
try {
    $lock = Enter-LauncherLock
    $state = Read-LauncherState
    if ($null -eq $state) { Write-Host 'AI Product Catalog Agent is not currently running.'; exit 0 }
    $services = @($state.services)
    [array]::Reverse($services)
    foreach ($service in $services) {
        if (Test-OwnedService $service) {
            Stop-OwnedService $service
            Write-Host "Stopped $($service.name) (PID $($service.pid))."
        } else { Write-Host "Skipped stale $($service.name) record; no unknown process stopped." }
    }
    $remaining = @($state.services | Where-Object { Test-OwnedService $_ })
    if ($remaining.Count -gt 0) { throw 'Some launcher-owned services remain alive. Runtime state retained; retry stop.ps1.' }
    Remove-LauncherState
    Write-Host 'AI Product Catalog Agent stopped.'
} catch { Write-Host "ERROR  $($_.Exception.Message)"; exit 1 }
finally { if ($null -ne $lock) { $lock.Dispose() } }
exit 0
