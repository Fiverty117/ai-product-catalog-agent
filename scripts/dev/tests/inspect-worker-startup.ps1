# Isolated metadata reproduction: never imports the app or opens a database.
param([int]$Launches = 3)
if ($Launches -lt 1 -or $Launches -gt 10) { throw 'Launches must be between 1 and 10.' }
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')
$logDirectory = Join-Path ([IO.Path]::GetTempPath()) ('catalog-launcher-metadata-' + [guid]::NewGuid().ToString('n'))
New-Item -ItemType Directory -Path $logDirectory | Out-Null
Write-Output "PowerShell $($PSVersionTable.PSVersion); logs: $logDirectory"
for ($launch = 0; $launch -lt $Launches; $launch++) {
    $service = [pscustomobject]@{
        id="isolated-$launch"; name='Isolated metadata worker'; kind='worker'
        executable=$script:PythonPath; directory=$script:BackendRoot
        arguments=@('-c','"import time; time.sleep(90)"','app.scripts.run_catalog_render_worker')
        marker='app.scripts.run_catalog_render_worker'; port=$null; url=$null
    }
    $record = Start-ManifestService $service $logDirectory
    $descendants = @()
    try {
        Wait-ServiceReady $record 2
        foreach ($sample in 0..2) {
            $fresh = Get-Process -Id $record.pid -ErrorAction Stop
            $observed = [ordered]@{
                launch=$launch; sample=$sample; pid=$record.pid
                recorded_start=$record.start_time_utc; fresh_start=$fresh.StartTime.ToUniversalTime().ToString('o')
                recorded_path=$record.executable; fresh_path=$fresh.Path
                start_matches=($fresh.StartTime.ToUniversalTime().ToString('o') -eq [string]$record.start_time_utc)
                path_matches=([string]$fresh.Path -eq [string]$record.executable)
            }
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)" -ErrorAction Stop
            $observed.cim_exists = $null -ne $cim
            $observed.command_line = $cim.CommandLine
            $observed.marker_matches = $cim.CommandLine -like "*$($record.marker)*"
            $observed.owned = $null -ne (Get-OwnedProcess $record)
            [pscustomobject]$observed | ConvertTo-Json -Compress
        }
        $descendants = @(Get-OwnedDescendants (Get-ProcessParents) $record.pid (ConvertTo-ProcessStartUtc $record.start_time_utc))
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $($record.pid)" -ErrorAction Stop |
            Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress
        if (-not (Test-OwnedService $record)) { throw 'Isolated worker was not recognized as owned.' }
    } finally {
        Stop-OwnedService $record
        foreach ($tracked in @($record) + $descendants) {
            $remaining = Get-Process -Id $tracked.pid -ErrorAction SilentlyContinue
            if ($null -ne $remaining -and $remaining.StartTime.ToUniversalTime() -eq (ConvertTo-ProcessStartUtc $tracked.start_time_utc)) {
                throw "Isolated test PID $($tracked.pid) did not stop. Logs: $logDirectory"
            }
        }
    }
}
Write-Output 'PASS isolated venv lifecycle: launch, identity, descendants, stop.'
