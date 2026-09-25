param([int]$BackendPort = 8001, [int]$FrontendPort = 5174)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

try {
    $info = Assert-Prerequisites $BackendPort $FrontendPort
    $pythonVersion = (& $script:PythonPath --version 2>&1 | Select-Object -First 1)
    $nodeVersion = (& (Get-NodePath) --version | Select-Object -First 1)
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npm) { throw 'npm is missing. Install Node.js/npm.' }
    $npmVersion = (& $npm.Source --version | Select-Object -First 1)
    Write-Host 'AI Product Catalog Agent — doctor'
    Write-Host "OK  $pythonVersion; backend virtualenv"
    Write-Host "OK  Node $nodeVersion; npm $npmVersion; frontend dependencies"
    Write-Host "OK  Database: $($info.database_path)"
    if ($info.migration_status -eq 'current') { Write-Host "OK  Migration: $($info.alembic_head) (head)" }
    else { Write-Host "WARN  Migration required (current: $($info.database_revision); head: $($info.alembic_head)). start.ps1 will upgrade it." }
    $portsFree = $true
    foreach ($entry in @(@{ port=$BackendPort; name='Backend' }, @{ port=$FrontendPort; name='Frontend' })) {
        try { Assert-PortAvailable $entry.port $entry.name; Write-Host "OK  $($entry.name) port $($entry.port) available" }
        catch { Write-Host "ERROR  $($_.Exception.Message)"; $portsFree = $false }
    }
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
        Write-Host 'WARN  OPENAI_API_KEY not configured. Product extraction, Product Copy and image enhancement workers will be skipped.'
        Write-Host '      Manual Product/Category management and catalog rendering remain available.'
    } else { Write-Host 'OK  OPENAI_API_KEY present for child processes (not validated; value hidden)' }
    if (-not $portsFree) { exit 1 }
} catch {
    Write-Host "ERROR  $($_.Exception.Message)"
    exit 1
}
exit 0
