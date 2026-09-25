param([int]$BackendPort = 8001, [int]$FrontendPort = 5174)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\dev\doctor.ps1') -BackendPort $BackendPort -FrontendPort $FrontendPort
exit $LASTEXITCODE
