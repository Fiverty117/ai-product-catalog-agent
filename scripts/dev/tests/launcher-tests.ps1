# Dependency-free, deterministic launcher tests. No app, DB, ports or real processes.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')
$script:DevScripts = Split-Path $PSScriptRoot -Parent
$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('catalog-launcher-tests-' + [guid]::NewGuid().ToString('n'))
New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
$script:Cases = 0

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Assert-Fails([scriptblock]$Action, [string]$Pattern) {
    try { & $Action } catch {
        Assert-True ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"
        return
    }
    throw "Expected failure: $Pattern"
}
function Reset-Fakes {
    $script:Fake = @{
        now=[datetime]'2026-09-26T00:00:00Z'; sleeps=0; processes=@{}; commands=@{}; parents=@{}
        stopped=@(); launched=@(); nextId=100; capturePathMissing=$false; captureMissingMarker=''; pendingUntil=0
        pendingMode=''; httpReady=$true; listener=@{}; failService=''; failStop=$false; workerChildren=$false
    }
    $script:RuntimeRoot = Join-Path $script:TestRoot "case-$script:Cases"
    $script:StatePath = Join-Path $script:RuntimeRoot 'state.json'
    $script:BackendRoot = $script:TestRoot
    $script:PythonPath = 'Invoke-FakePython'
}
function Get-Date { return $script:Fake.now }
function Start-Sleep([int]$Milliseconds) {
    $script:Fake.sleeps += 1
    $script:Fake.now = $script:Fake.now.AddMilliseconds($Milliseconds)
}
function Get-Process {
    [CmdletBinding()]param([int]$Id)
    if (-not $script:Fake.processes.ContainsKey($Id)) {
        $errorRecord = New-Object Management.Automation.ErrorRecord (
            (New-Object ArgumentException 'Process not found'), 'NoProcessFoundForGivenId',
            [Management.Automation.ErrorCategory]::ObjectNotFound, $Id)
        $PSCmdlet.WriteError($errorRecord)
        return
    }
    $process = $script:Fake.processes[$Id]
    if ($script:Fake.capturePathMissing) {
        $script:Fake.capturePathMissing = $false
        return [pscustomobject]@{Id=$Id; StartTime=$process.StartTime; Path=$null}
    }
    if ($script:Fake.pendingMode -eq 'path' -and $script:Fake.sleeps -lt $script:Fake.pendingUntil) {
        return [pscustomobject]@{Id=$Id; StartTime=$process.StartTime; Path=$null}
    }
    return $process
}
function Get-CimInstance {
    [CmdletBinding()]param([string]$ClassName, [string]$Filter)
    $processId = [int]($Filter -replace '\D','')
    if ($script:Fake.sleeps -lt $script:Fake.pendingUntil) {
        switch ($script:Fake.pendingMode) {
            'cim' { return $null }
            'command' { return [pscustomobject]@{CommandLine=''} }
            'cim-error' { throw 'CIM temporarily unavailable' }
        }
    }
    return [pscustomobject]@{CommandLine=$script:Fake.commands[$processId]}
}
function Stop-Process {
    [CmdletBinding()]param([int]$Id, [switch]$Force)
    if ($script:Fake.failStop) { throw 'Isolated stop failure' }
    $script:Fake.stopped += $Id
    $script:Fake.processes.Remove($Id)
}
function Start-Process {
    param($FilePath,$ArgumentList,$WorkingDirectory,$RedirectStandardOutput,$RedirectStandardError,$WindowStyle,[switch]$PassThru)
    $script:Fake.nextId += 1
    $processId = $script:Fake.nextId
    $process = [pscustomobject]@{Id=$processId; StartTime=$script:Fake.now; Path=[IO.Path]::GetFullPath([string]$FilePath)}
    $script:Fake.processes[$processId] = $process
    $script:Fake.commands[$processId] = "$FilePath $ArgumentList"
    $script:Fake.launched += $processId
    if ($script:Fake.captureMissingMarker -and $ArgumentList -like "*$($script:Fake.captureMissingMarker)*") {
        $script:Fake.capturePathMissing = $true
    }
    if ($script:Fake.workerChildren -and $ArgumentList -like '*app.scripts.run_*worker*') {
        $childId = $processId + 1000
        $script:Fake.processes[$childId] = [pscustomobject]@{Id=$childId; StartTime=$script:Fake.now.AddMilliseconds(1); Path='C:\fake\base\python.exe'}
        $script:Fake.commands[$childId] = $ArgumentList
        $script:Fake.parents[$childId] = $processId
    }
    if ($ArgumentList -like '*--port*') {
        $port = [int]([regex]::Match($ArgumentList,'--port (\d+)').Groups[1].Value)
        $script:Fake.listener[$port] = $processId
    }
    if ($ArgumentList -like "*$($script:Fake.failService)*" -and $script:Fake.failService) {
        $script:Fake.processes.Remove($processId)
    }
    return $process
}
function Get-ProcessParents { return $script:Fake.parents }
function Get-NodePath { return 'C:\fake\node.exe' }
function Get-PortOccupant([int]$Port) {
    if ($script:Fake.listener.ContainsKey($Port)) { return [pscustomobject]@{pid=$script:Fake.listener[$Port]; name='fake'} }
    return $null
}
function Invoke-WebRequest {
    param($Uri,[switch]$UseBasicParsing,$TimeoutSec,$ErrorAction)
    return [pscustomobject]@{StatusCode=$(if ($script:Fake.httpReady) {200} else {503})}
}
function Enter-LauncherLock {
    New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null
    $lock = New-Object psobject
    $lock | Add-Member ScriptMethod Dispose {}
    return $lock
}
function Assert-Prerequisites { return (Get-RuntimeInfo) }
function Assert-PortAvailable { }
function Get-RuntimeInfo {
    return [pscustomobject]@{database_path='unavailable'; migration_status='current'}
}
function Invoke-FakePython { $global:LASTEXITCODE = 0 }

function Invoke-LauncherScript([string]$Name) {
    # Execute the actual orchestration in this fake scope; replace only bootstrap
    # and host exits, so no nested common.ps1 load can bypass the fakes.
    $source = Get-Content -LiteralPath (Join-Path $script:DevScripts "$Name.ps1") -Raw
    $source = $source.Replace(". (Join-Path `$PSScriptRoot 'common.ps1')",'')
    $source = $source.Replace('exit 1', "throw 'Launcher exit 1'").Replace('exit 0','return')
    & ([scriptblock]::Create($source))
}
function New-Worker {
    $service = @(Get-ServiceManifest 8001 5184 $true | Where-Object { $_.id -eq 'catalog-render' })[0]
    $service.executable = 'C:\fake\venv\python.exe'
    return (Start-ManifestService $service $script:TestRoot)
}
function Add-Child($Record, [int]$ChildId = 500) {
    $script:Fake.processes[$ChildId] = [pscustomobject]@{
        Id=$ChildId; StartTime=$script:Fake.now.AddMilliseconds(1); Path='C:\fake\base\python.exe'
    }
    $script:Fake.commands[$ChildId] = "python.exe -m $($Record.marker)"
    $script:Fake.parents[$ChildId] = [int]$Record.pid
}
function Save-State($Records) {
    New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null
    [pscustomobject]@{
        services=@($Records); skipped=@(); backend_port=8001; frontend_port=5184
        database_path='unavailable'; openai_configured=$false
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
}
function Test-Case([string]$Name, [scriptblock]$Body) {
    Reset-Fakes
    & $Body
    $script:Cases += 1
    Write-Output "PASS $Name"
}

Test-Case 'launch capture has null Path, but record anchors requested executable' {
    $script:Fake.capturePathMissing = $true
    $record = New-Worker
    Assert-True ($record.executable -eq 'C:\fake\venv\python.exe') 'Captured null path instead of manifest identity'
    Wait-ServiceReady $record 2
    Assert-True ($script:Fake.sleeps -eq 0) 'Unnecessary startup sleep'
}
foreach ($mode in @('path','cim','command','cim-error')) {
    Test-Case "renderer retries transient $mode metadata" {
        $record = New-Worker
        $script:Fake.pendingMode = $mode; $script:Fake.pendingUntil = 1
        Assert-True ($null -eq (Get-OwnedProcess $record)) 'Unverified identity was accepted'
        Wait-ServiceReady $record 2
        Assert-True ($script:Fake.sleeps -eq 1) 'Did not retry exactly once'
        Assert-True (Test-OwnedService $record) 'Worker is not owned after readiness'
    }
}
Test-Case 'permanently incomplete metadata times out, never succeeds' {
    $record = New-Worker
    $script:Fake.pendingMode='cim'; $script:Fake.pendingUntil=100
    Assert-Fails { Wait-ServiceReady $record 2 } '*ownership could not be verified*Win32_Process*'
    Assert-True ($script:Fake.sleeps -eq 5) 'Unbounded or wrong retry deadline'
}
Test-Case 'actual renderer exit fails immediately' {
    $record = New-Worker
    $script:Fake.processes.Remove([int]$record.pid)
    Assert-Fails { Wait-ServiceReady $record 2 } '*exited during startup*'
    Assert-True ($script:Fake.sleeps -eq 0) 'Dead process was retried'
}
foreach ($mismatch in @('start','path','marker')) {
    Test-Case "ownership mismatch $mismatch is rejected without stopping unrelated Python" {
        $record = New-Worker
        switch ($mismatch) {
            'start' { $script:Fake.processes[[int]$record.pid].StartTime = $script:Fake.now.AddTicks(1) }
            'path' { $script:Fake.processes[[int]$record.pid].Path = 'C:\unrelated\python.exe' }
            'marker' { $script:Fake.commands[[int]$record.pid] = 'python.exe unrelated.py' }
        }
        Assert-Fails { Wait-ServiceReady $record 2 } '*ownership mismatch*'
        Stop-OwnedService $record
        Assert-True ($script:Fake.stopped.Count -eq 0) 'Stopped an unrelated/reused PID'
        Assert-True ($script:Fake.sleeps -eq 0) 'Definitive mismatch retried'
    }
}
foreach ($serviceId in @('catalog-render','product-extraction','product-copy','image-enhancement')) {
    Test-Case "$serviceId uses bounded worker readiness" {
        $service = @(Get-ServiceManifest 8001 5184 $true | Where-Object { $_.id -eq $serviceId })[0]
        $service.executable = 'C:\fake\venv\python.exe'
        $record = Start-ManifestService $service $script:TestRoot
        $script:Fake.pendingMode='command'; $script:Fake.pendingUntil=1
        Wait-ServiceReady $record 2
        Assert-True (Test-OwnedService $record) 'Worker not owned'
    }
}
foreach ($serviceId in @('backend','frontend')) {
    Test-Case "$serviceId accepts a live-root child listener with a different executable and safely stops its tree" {
        $service = @(Get-ServiceManifest 8001 5184 $true | Where-Object { $_.id -eq $serviceId })[0]
        $record = Start-ManifestService $service $script:TestRoot
        Add-Child $record
        $script:Fake.listener[[int]$record.port] = 500
        $script:Fake.processes[900] = [pscustomobject]@{Id=900; StartTime=$script:Fake.now; Path='C:\unrelated\python.exe'}
        $script:Fake.commands[900] = "python.exe $($record.marker)"
        Wait-ServiceReady $record 2
        Assert-True ($script:Fake.sleeps -eq 0) 'Healthy related HTTP child falsely waited'
        Assert-True ($script:Fake.processes[500].Path -ne $record.executable) 'Fixture did not use a different child executable'
        Save-State @($record)
        $lines = @(Invoke-LauncherScript 'status' 6>&1 | ForEach-Object { "$_" })
        Assert-True (($lines -join "`n") -like "*$($service.name)*Running*") 'HTTP status did not recognize healthy child'
        Invoke-LauncherScript 'stop'
        Assert-True ($record.pid -in $script:Fake.stopped -and 500 -in $script:Fake.stopped) 'HTTP owned tree leaked'
        Assert-True ($script:Fake.processes.ContainsKey(900)) 'Unrelated matching process killed'
    }
    Test-Case "$serviceId rejects healthy unrelated listener and reports the failed ancestry predicate" {
        $service = @(Get-ServiceManifest 8001 5184 $true | Where-Object { $_.id -eq $serviceId })[0]
        $record = Start-ManifestService $service $script:TestRoot
        $script:Fake.processes[900] = [pscustomobject]@{Id=900; StartTime=$script:Fake.now; Path='C:\unrelated\python.exe'}
        $script:Fake.commands[900] = "python.exe $($record.marker)"
        $script:Fake.listener[[int]$record.port] = 900
        Assert-Fails { Wait-ServiceReady $record 2 } '*did not become ready*health=HTTP 200*listener=900*related=False*'
        Assert-True ($record.pid -ne 900) 'Unrelated listener adopted'
        Stop-OwnedService $record
        Assert-True ($script:Fake.processes.ContainsKey(900)) 'Unrelated listener stopped'
    }
    Test-Case "$serviceId HTTP readiness retains listener/health checks" {
        $service = @(Get-ServiceManifest 8001 5184 $true | Where-Object { $_.id -eq $serviceId })[0]
        $record = Start-ManifestService $service $script:TestRoot
        Wait-ServiceReady $record 2
        $script:Fake.httpReady=$false
        Assert-Fails { Wait-ServiceReady $record 2 } '*did not become ready*health=HTTP 503*root=owned*'
    }
    Test-Case "$serviceId adopts only related HTTP listener child" {
        $service = @(Get-ServiceManifest 8001 5184 $true | Where-Object { $_.id -eq $serviceId })[0]
        $record = Start-ManifestService $service $script:TestRoot
        Add-Child $record
        $record.lineage = @(Get-OwnedDescendants $script:Fake.parents $record.pid $script:Fake.now)
        $script:Fake.listener[[int]$record.port] = 500
        $script:Fake.processes.Remove([int]$record.pid)
        Wait-ServiceReady $record 2
        Assert-True ($record.pid -eq 500) 'Related listener not adopted'
        Assert-True (Test-OwnedService $record) 'Adopted HTTP child not recognized'
        $script:Fake.listener[[int]$record.port] = 900
        Assert-True (-not (Try-AdoptHttpChild $record)) 'Adopted unrelated listener'
    }
}
Test-Case 'workers never adopt a matching unrelated or orphaned descendant' {
    $record = New-Worker
    Add-Child $record
    $script:Fake.processes.Remove([int]$record.pid)
    Assert-Fails { Wait-ServiceReady $record 2 } '*exited during startup*'
    Assert-True ($record.pid -ne 500) 'Worker child was adopted without proof'
}
Test-Case 'status recognizes worker after persisted state roundtrip' {
    $record = New-Worker
    Wait-ServiceReady $record 2
    Save-State @($record)
    $lines = @(Invoke-LauncherScript 'status' 6>&1 | ForEach-Object { "$_" })
    Assert-True (($lines -join "`n") -like '*Catalog rendering*Running*') 'Persisted worker status is not Running'
}
Test-Case 'stop terminates verified worker tree and preserves unrelated Python' {
    $record = New-Worker
    Add-Child $record
    $script:Fake.processes[900] = [pscustomobject]@{Id=900; StartTime=$script:Fake.now; Path='C:\fake\base\python.exe'}
    $script:Fake.commands[900] = "python.exe -m $($record.marker)"
    Save-State @($record)
    Invoke-LauncherScript 'stop'
    Assert-True ($record.pid -in $script:Fake.stopped -and 500 -in $script:Fake.stopped) 'Owned tree leaked'
    Assert-True ($script:Fake.processes.ContainsKey(900)) 'Unrelated Python stopped'
    Assert-True (-not (Test-Path $script:StatePath)) 'Stopped state retained'
}
Test-Case 'stop retries transient metadata before verified termination' {
    $record = New-Worker
    $script:Fake.pendingMode='cim'; $script:Fake.pendingUntil=1
    Stop-OwnedService $record
    Assert-True ($script:Fake.sleeps -eq 1 -and $record.pid -in $script:Fake.stopped) 'Transient stop lookup leaked worker'
}
Test-Case 'may-be-alive check retains a worker when a second lookup becomes verified' {
    $record = New-Worker
    function Test-OwnedService { return $false }
    Assert-True (Test-ServiceMayBeAlive $record) 'Second verified lookup incorrectly treated as dead'
}
Test-Case 'unverifiable status/stop retain state and never kill' {
    $record = New-Worker
    Save-State @($record)
    $script:Fake.pendingMode='cim-error'; $script:Fake.pendingUntil=100
    $lines = @(Invoke-LauncherScript 'status' 6>&1 | ForEach-Object { "$_" })
    Assert-True (($lines -join "`n") -like '*Catalog rendering*Unverified*') 'Unverified worker misreported dead'
    Assert-Fails { Invoke-LauncherScript 'stop' } '*Launcher exit 1*'
    Assert-True ((Test-Path $script:StatePath) -and $script:Fake.stopped.Count -eq 0) 'Unverified state removed or process killed'
}
Test-Case 'start rolls back earlier services when renderer genuinely exits' {
    $script:Fake.failService='app.scripts.run_catalog_render_worker'
    Assert-Fails { Invoke-LauncherScript 'start' } '*Launcher exit 1*'
    Assert-True ($script:Fake.stopped.Count -eq 1) 'Backend was not rolled back'
    Assert-True ($script:Fake.processes.Count -eq 0) 'Failure leaked a process'
}
Test-Case 'later failure rolls back renderer despite initial captured null Path' {
    $script:Fake.captureMissingMarker='app.scripts.run_catalog_render_worker'
    $script:Fake.workerChildren=$true
    $script:Fake.failService='vite.js'
    Assert-Fails { Invoke-LauncherScript 'start' } '*Launcher exit 1*'
    Assert-True ($script:Fake.processes.Count -eq 0) 'Later failure leaked workers'
    Assert-True ($script:Fake.stopped.Count -ge 2) 'Earlier renderer/backend not rolled back'
    Assert-True ($script:Fake.stopped.Count -gt ($script:Fake.launched.Count - 1)) 'Worker descendants not rolled back'
}
Test-Case 'failed rollback preserves partial owned state' {
    $script:Fake.failService='vite.js'; $script:Fake.failStop=$true
    Assert-Fails { Invoke-LauncherScript 'start' } '*Launcher exit 1*'
    Assert-True (Test-Path $script:StatePath) 'Partial owned state lost'
}
Test-Case 'unverifiable prior state blocks duplicate start and remains recoverable' {
    $record = New-Worker
    Save-State @($record)
    $script:Fake.pendingMode='cim'; $script:Fake.pendingUntil=100
    Assert-Fails { Invoke-LauncherScript 'start' } '*Launcher exit 1*'
    Assert-True ((Test-Path $script:StatePath) -and $script:Fake.launched.Count -eq 1) 'Prior state lost or duplicate started'
}
Test-Case 'successful start and repeated start do not duplicate processes' {
    Invoke-LauncherScript 'start'
    $launchCount = $script:Fake.launched.Count
    Invoke-LauncherScript 'start'
    Assert-True ($script:Fake.launched.Count -eq $launchCount) 'Duplicate services launched'
    Invoke-LauncherScript 'stop'
    Assert-True ($script:Fake.processes.Count -eq 0) 'Successful runtime leaked processes'
}
Write-Output "$script:Cases launcher tests passed; all process/CIM/time operations were fake."
