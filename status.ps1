Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\dev\status.ps1')
exit $LASTEXITCODE
