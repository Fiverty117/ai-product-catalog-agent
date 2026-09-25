param([int]$BackendPort = 8001, [int]$FrontendPort = 5174, [switch]$OpenBrowser)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\dev\start.ps1') -BackendPort $BackendPort -FrontendPort $FrontendPort -OpenBrowser:$OpenBrowser
exit $LASTEXITCODE
