Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\dev\stop.ps1')
exit $LASTEXITCODE
