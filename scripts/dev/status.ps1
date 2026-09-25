Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    $state = Read-LauncherState
    if ($null -eq $state) { Write-Host 'AI Product Catalog Agent is not currently running.'; exit 0 }
    Write-Host 'AI Product Catalog Agent — status'
    Write-Host ('{0,-24} {1,-12} {2,-8} {3}' -f 'Service','Status','PID','Port')
    $running = 0
    foreach ($service in @($state.services)) {
        $owned = Test-OwnedService $service
        $status = 'Stale'
        if ($owned) {
            $healthy = $true
            if ($service.kind -eq 'http') {
                $listener = Get-PortOccupant ([int]$service.port)
                $healthy = (Test-HttpReady $service.url) -and $null -ne $listener -and (Test-ListenerRelated $service $listener.pid)
            }
            $status = if ($healthy) { 'Running' } else { 'Unhealthy' }
            $running += 1
        }
        $portText = if ($null -eq $service.port) { '-' } else { [string]$service.port }
        Write-Host ('{0,-24} {1,-12} {2,-8} {3}' -f $service.name,$status,$service.pid,$portText)
    }
    foreach ($name in @($state.skipped)) { Write-Host ('{0,-24} {1}' -f $name,'Skipped — OPENAI_API_KEY missing') }
    if ($running -eq 0) { Write-Host 'Application not running. Stale runtime metadata detected; start.ps1 can clear it safely.' }
    else {
        Write-Host "OpenAI key at launch: $(if ($state.openai_configured) { 'present (not validated)' } else { 'not configured' })"
        Write-Host "Database: $($state.database_path)"
        if ($state.database_path -ne 'unavailable') {
            $env:DATABASE_URL = 'sqlite:///' + ([string]$state.database_path).Replace('\', '/')
            try {
                $info = Get-RuntimeInfo
                Write-Host "Migration: $($info.database_revision) / head $($info.alembic_head) ($($info.migration_status))"
            } catch { Write-Host 'Migration: unavailable' }
        }
        Write-Host "Application: http://127.0.0.1:$($state.frontend_port)"
    }
} catch { Write-Host "ERROR  $($_.Exception.Message)"; exit 1 }
exit 0
