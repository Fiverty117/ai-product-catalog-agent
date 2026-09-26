Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$script:BackendRoot = Join-Path $script:ProjectRoot 'backend'
$script:FrontendRoot = Join-Path $script:ProjectRoot 'frontend'
$script:RuntimeRoot = Join-Path $script:ProjectRoot '.runtime'
$script:StatePath = Join-Path $script:RuntimeRoot 'state.json'
$script:PythonPath = Join-Path $script:BackendRoot '.venv\Scripts\python.exe'
$script:VitePath = Join-Path $script:FrontendRoot 'node_modules\vite\bin\vite.js'

if (-not ('LocalCatalogProcessTree' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class LocalCatalogProcessTree {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct Entry {
        public uint Size, Usage, ProcessId;
        public IntPtr DefaultHeap;
        public uint ModuleId, Threads, ParentId;
        public int Priority;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string ExeFile;
    }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);
    [DllImport("kernel32.dll", EntryPoint = "Process32FirstW", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool First(IntPtr snapshot, ref Entry entry);
    [DllImport("kernel32.dll", EntryPoint = "Process32NextW", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool Next(IntPtr snapshot, ref Entry entry);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
    public static Dictionary<int,int> Parents() {
        var result = new Dictionary<int,int>();
        IntPtr snapshot = CreateToolhelp32Snapshot(2, 0);
        if (snapshot == new IntPtr(-1)) throw new InvalidOperationException("Process tree snapshot unavailable");
        try {
            Entry entry = new Entry(); entry.Size = (uint)Marshal.SizeOf(typeof(Entry));
            if (First(snapshot, ref entry)) do { result[(int)entry.ProcessId] = (int)entry.ParentId; } while (Next(snapshot, ref entry));
        } finally { CloseHandle(snapshot); }
        return result;
    }
}
'@
}

function Assert-PortNumber([int]$Port, [string]$Name) {
    if ($Port -lt 1 -or $Port -gt 65535) { throw "$Name must be between 1 and 65535." }
}

function Get-NodePath {
    $command = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw 'Node.js is missing. Install Node.js, then run doctor.ps1 again.' }
    return $command.Source
}

function Set-LauncherEnvironment([int]$BackendPort) {
    if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
        $databasePath = (Join-Path $script:BackendRoot 'catalog.db').Replace('\', '/')
        $env:DATABASE_URL = "sqlite:///$databasePath"
    }
    $env:VITE_API_TARGET = "http://127.0.0.1:$BackendPort"
}

function Get-RuntimeInfo {
    Push-Location $script:BackendRoot
    try {
        $output = & $script:PythonPath -m app.scripts.local_runtime_info 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Database configuration cannot be read. Check DATABASE_URL and backend dependencies.' }
        return ($output | Select-Object -Last 1 | ConvertFrom-Json)
    } finally { Pop-Location }
}

function Get-ServiceManifest([int]$BackendPort, [int]$FrontendPort, [bool]$OpenAIConfigured) {
    $node = Get-NodePath
    $services = @(
        [pscustomobject]@{ id='backend'; name='Backend'; kind='http'; executable=$script:PythonPath; arguments=@('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$BackendPort"); directory=$script:BackendRoot; port=$BackendPort; marker='app.main:app'; requires_openai=$false; url="http://127.0.0.1:$BackendPort/health" },
        [pscustomobject]@{ id='catalog-render'; name='Catalog rendering'; kind='worker'; executable=$script:PythonPath; arguments=@('-m','app.scripts.run_catalog_render_worker'); directory=$script:BackendRoot; port=$null; marker='app.scripts.run_catalog_render_worker'; requires_openai=$false; url=$null },
        [pscustomobject]@{ id='product-extraction'; name='Product extraction'; kind='worker'; executable=$script:PythonPath; arguments=@('-m','app.scripts.run_product_intake_worker'); directory=$script:BackendRoot; port=$null; marker='app.scripts.run_product_intake_worker'; requires_openai=$true; url=$null },
        [pscustomobject]@{ id='product-copy'; name='Product Copy'; kind='worker'; executable=$script:PythonPath; arguments=@('-m','app.scripts.run_product_copy_worker'); directory=$script:BackendRoot; port=$null; marker='app.scripts.run_product_copy_worker'; requires_openai=$true; url=$null },
        [pscustomobject]@{ id='image-enhancement'; name='Image enhancement'; kind='worker'; executable=$script:PythonPath; arguments=@('-m','app.scripts.run_image_enhancement_worker'); directory=$script:BackendRoot; port=$null; marker='app.scripts.run_image_enhancement_worker'; requires_openai=$true; url=$null },
        [pscustomobject]@{ id='frontend'; name='Frontend'; kind='http'; executable=$node; arguments=@(('"{0}"' -f $script:VitePath),'--host','127.0.0.1','--port',"$FrontendPort",'--strictPort'); directory=$script:FrontendRoot; port=$FrontendPort; marker='vite.js'; requires_openai=$false; url="http://127.0.0.1:$FrontendPort/" }
    )
    if ($OpenAIConfigured) { return $services }
    return @($services | Where-Object { -not $_.requires_openai })
}

function Get-PortOccupant([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '0.0.0.0' } |
        Select-Object -First 1
    $ownerPid = $null
    if ($null -ne $listener) { $ownerPid = [int]$listener.OwningProcess }
    if ($null -eq $ownerPid) {
        foreach ($line in @(netstat -ano -p TCP)) {
            if ($line -match "^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0):$Port\s+\S+\s+\S+\s+(\d+)\s*$") {
                $ownerPid = [int]$Matches[1]
                break
            }
        }
    }
    if ($null -eq $ownerPid) { return $null }
    $name = 'unknown'
    try { $name = (Get-Process -Id $ownerPid -ErrorAction Stop).ProcessName } catch { }
    return [pscustomobject]@{ pid=$ownerPid; name=$name }
}

function Assert-PortAvailable([int]$Port, [string]$Service) {
    $socket = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
    try { $socket.Start() }
    catch {
        $occupant = Get-PortOccupant $Port
        $description = if ($null -eq $occupant) { 'PID/process unavailable' } else { "PID $($occupant.pid), process $($occupant.name)" }
        throw "$Service port $Port is already in use ($description). No process was stopped. Resolve the conflict or choose another port."
    }
    finally { try { $socket.Stop() } catch { } }
}

function Read-LauncherState {
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json) }
    catch { throw 'Runtime state is unreadable. No process will be stopped; inspect .runtime/state.json manually.' }
}

function Remove-LauncherState {
    if (Test-Path -LiteralPath $script:StatePath -PathType Leaf) {
        Remove-Item -LiteralPath $script:StatePath -Force
    }
}

function ConvertTo-ProcessStartUtc($Value) {
    # PowerShell 7 may deserialize an ISO JSON string as DateTime; retain its ticks.
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    return [datetime]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
}

function Get-ProcessOwnership($Record) {
    $state = [pscustomobject]@{ status='unverified'; reason='Process metadata unavailable'; process=$null }
    try { $process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -like 'NoProcessFoundForGivenId*') {
            $state.status = 'dead'; $state.reason = 'Launch PID no longer exists'
        }
        return $state
    }
    try {
        if ($process.StartTime.ToUniversalTime() -ne (ConvertTo-ProcessStartUtc $Record.start_time_utc)) {
            $state.status = 'mismatch'; $state.reason = 'StartTime differs (possible PID reuse)'; return $state
        }
        $path = [string]$process.Path
        if ([string]::IsNullOrWhiteSpace($path)) { $state.reason = 'Process.Path unavailable'; return $state }
        if (-not [string]::Equals($path, [string]$Record.executable, [System.StringComparison]::OrdinalIgnoreCase)) {
            $state.status = 'mismatch'; $state.reason = 'Executable path differs'; return $state
        }
        $state.reason = 'Win32_Process/CommandLine unavailable'
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($Record.pid)" -ErrorAction Stop
        if ($null -eq $cim -or [string]::IsNullOrWhiteSpace([string]$cim.CommandLine)) { return $state }
        if ([string]$cim.CommandLine -notlike "*$($Record.marker)*") {
            $state.status = 'mismatch'; $state.reason = 'Command marker differs'; return $state
        }
        $state.status = 'owned'; $state.reason = 'Identity verified'; $state.process = $process
    } catch { }
    return $state
}

function Get-OwnedProcess($Record, [int]$VerificationSeconds = 0) {
    $deadline = (Get-Date).AddSeconds($VerificationSeconds)
    do {
        $identity = Get-ProcessOwnership $Record
        if ($identity.status -eq 'owned') { return $identity.process }
        if ($identity.status -ne 'unverified' -or $VerificationSeconds -eq 0) { return $null }
        if ((Get-Date) -ge $deadline) { throw "Cannot verify $($Record.name) (PID $($Record.pid)): $($identity.reason). Runtime state must be retained." }
        Start-Sleep -Milliseconds 400
    } while ($true)
}

function Test-ServiceMayBeAlive($Record) {
    if (Test-OwnedService $Record) { return $true }
    return (Get-ProcessOwnership $Record).status -in @('owned','unverified')
}

function Get-ProcessParents {
    return [LocalCatalogProcessTree]::Parents()
}

function Test-OwnedService($Record) {
    if ($null -ne (Get-OwnedProcess $Record)) { return $true }
    if ($Record.kind -eq 'http' -and (Try-AdoptHttpChild $Record)) { return $true }
    if ($Record.PSObject.Properties.Name -contains 'lineage') {
        foreach ($child in @($Record.lineage)) {
            try {
                $fresh = Get-Process -Id ([int]$child.pid) -ErrorAction Stop
                if ($fresh.StartTime.ToUniversalTime() -eq (ConvertTo-ProcessStartUtc $child.start_time_utc) -and
                    [string]::Equals([string]$fresh.Path, [string]$child.executable, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
            } catch { }
        }
    }
    return $false
}

function Get-OwnedDescendants($Map, [int]$ParentId, [datetime]$ParentStart) {
    foreach ($entry in $Map.GetEnumerator()) {
        if ([int]$entry.Value -ne $ParentId) { continue }
        try {
            $child = Get-Process -Id ([int]$entry.Key) -ErrorAction Stop
            $childStart = $child.StartTime.ToUniversalTime()
            if ($childStart -lt $ParentStart.ToUniversalTime()) { continue }
            Get-OwnedDescendants $Map $child.Id $child.StartTime
            [pscustomobject]@{ pid=$child.Id; start_time_utc=$childStart.ToString('o'); executable=$child.Path }
        } catch { continue }
    }
}

function Stop-OwnedService($Record) {
    $process = Get-OwnedProcess $Record 2
    if ($null -eq $process -and $Record.kind -eq 'http') {
        $null = Try-AdoptHttpChild $Record
        $process = Get-OwnedProcess $Record
    }
    $children = @()
    if ($null -ne $process) {
        $parents = Get-ProcessParents
        $children = @(Get-OwnedDescendants $parents $process.Id $process.StartTime)
        if ($null -ne (Get-OwnedProcess $Record)) { Stop-Process -Id ([int]$Record.pid) -Force -ErrorAction Stop }
    }
    if ($Record.PSObject.Properties.Name -contains 'lineage') { $children += @($Record.lineage) }
    foreach ($child in $children) {
        try {
            $fresh = Get-Process -Id ([int]$child.pid) -ErrorAction Stop
            if ($fresh.StartTime.ToUniversalTime() -eq (ConvertTo-ProcessStartUtc $child.start_time_utc) -and [string]::Equals([string]$fresh.Path, [string]$child.executable, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id ([int]$child.pid) -Force -ErrorAction Stop
            }
        } catch {
            if (Get-Process -Id ([int]$child.pid) -ErrorAction SilentlyContinue) { throw }
        }
    }
}

function Get-HttpReadiness([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return [pscustomobject]@{ ready=($response.StatusCode -eq 200); reason="HTTP $($response.StatusCode)" }
    } catch {
        return [pscustomobject]@{ ready=$false; reason="$($_.Exception.GetType().Name): $($_.Exception.Message)" }
    }
}

function Test-HttpReady([string]$Url) {
    return (Get-HttpReadiness $Url).ready
}

function Test-ProcessDescendsFrom($Map, [int]$ProcessId, [int]$AncestorId) {
    $current = $ProcessId
    for ($depth = 0; $depth -lt 16; $depth++) {
        if (-not $Map.ContainsKey($current)) { return $false }
        $current = [int]$Map[$current]
        if ($current -eq $AncestorId) { return $true }
    }
    return $false
}

function Test-ListenerRelated($Record, [int]$ListenerId) {
    $parents = Get-ProcessParents
    $ancestors = @([int]$Record.pid)
    if ($Record.PSObject.Properties.Name -contains 'lineage') { $ancestors += @($Record.lineage | ForEach-Object { [int]$_.pid }) }
    foreach ($ancestor in $ancestors) {
        if ($ListenerId -eq $ancestor -or (Test-ProcessDescendsFrom $parents $ListenerId $ancestor)) { return $true }
    }
    return $false
}

function Try-AdoptHttpChild($Record) {
    $listener = Get-PortOccupant ([int]$Record.port)
    if ($null -eq $listener -or $listener.pid -eq [int]$Record.pid) { return $false }
    if (-not (Test-ListenerRelated $Record $listener.pid)) { return $false }
    try {
        $child = Get-Process -Id $listener.pid -ErrorAction Stop
        if ($child.StartTime.ToUniversalTime() -lt (ConvertTo-ProcessStartUtc $Record.start_time_utc)) { return $false }
        $Record.pid = $child.Id
        $Record.start_time_utc = $child.StartTime.ToUniversalTime().ToString('o')
        $Record.executable = $child.Path
        $Record.marker = if ($Record.id -eq 'frontend') { 'vite.js' } else { $Record.marker }
        return $true
    } catch { return $false }
}

function Wait-ServiceReady($Record, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    $httpDiagnostic = 'No HTTP check completed'
    while ((Get-Date) -lt $deadline) {
        if ($Record.kind -eq 'worker') {
            $identity = Get-ProcessOwnership $Record
            if ($identity.status -eq 'owned') { return }
            if ($identity.status -eq 'dead') { throw "$($Record.name) exited during startup (PID $($Record.pid)). See $($Record.error_log)." }
            if ($identity.status -eq 'mismatch') { throw "$($Record.name) ownership mismatch (PID $($Record.pid)): $($identity.reason). No unrelated process accepted. See $($Record.error_log)." }
            Start-Sleep -Milliseconds 400
            continue
        }
        $rootIdentity = Get-ProcessOwnership $Record
        $root = $rootIdentity.process
        if ($null -ne $root -and $Record.PSObject.Properties.Name -contains 'lineage') {
            $parents = Get-ProcessParents
            foreach ($child in @(Get-OwnedDescendants $parents $root.Id $root.StartTime)) {
                if ($null -ne $child -and $child.pid -notin @($Record.lineage | ForEach-Object { $_.pid })) {
                    $Record.lineage += $child
                }
            }
        }
        $http = Get-HttpReadiness $Record.url
        $httpDiagnostic = "health=$($http.reason); root=$($rootIdentity.status) ($($rootIdentity.reason)); listener=not checked; related=not checked; lineage=$(@($Record.lineage).Count)"
        if ($http.ready) {
            $listener = Get-PortOccupant ([int]$Record.port)
            $related = $false
            if ($null -ne $listener) { $related = Test-ListenerRelated $Record $listener.pid }
            $listenerText = if ($null -eq $listener) { 'none' } else { [string]$listener.pid }
            $httpDiagnostic = "health=$($http.reason); root=$($rootIdentity.status) ($($rootIdentity.reason)); listener=$listenerText; related=$related; lineage=$(@($Record.lineage).Count)"
            if ($null -ne $listener -and $related) {
                if ($null -ne $root) { return }
                if (Try-AdoptHttpChild $Record) { return }
                $httpDiagnostic += '; adoption=False'
            }
        }
        Start-Sleep -Milliseconds 400
    }
    if ($Record.kind -eq 'worker') {
        throw "$($Record.name) ownership could not be verified in $Seconds seconds (launch PID $($Record.pid)): $($identity.reason). See $($Record.error_log)."
    }
    throw "$($Record.name) did not become ready in $Seconds seconds (launch PID $($Record.pid)). $httpDiagnostic. See $($Record.error_log)."
}

function Start-ManifestService($Service, [string]$RunLogDirectory) {
    $out = Join-Path $RunLogDirectory "$($Service.id).out.log"
    $err = Join-Path $RunLogDirectory "$($Service.id).err.log"
    $arguments = [string]::Join(' ', [string[]]$Service.arguments)
    $started = Start-Process -FilePath $Service.executable -ArgumentList $arguments -WorkingDirectory $Service.directory -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    $process = Get-Process -Id $started.Id -ErrorAction Stop
    return [pscustomobject]@{
        id=$Service.id; name=$Service.name; kind=$Service.kind; pid=$process.Id
        start_time_utc=$process.StartTime.ToUniversalTime().ToString('o')
        # Process.Path can be null immediately after Start-Process on Windows.
        # Anchor ownership to the requested executable, then verify the live path.
        executable=[System.IO.Path]::GetFullPath([string]$Service.executable); marker=$Service.marker; port=$Service.port; url=$Service.url
        output_log=$out; error_log=$err; working_directory=$Service.directory; lineage=@()
    }
}

function Enter-LauncherLock {
    New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null
    $path = Join-Path $script:RuntimeRoot 'launcher.lock'
    try { return [System.IO.File]::Open($path, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None) }
    catch { throw 'Another launcher command is running. Wait and retry.' }
}

function Assert-Prerequisites([int]$BackendPort, [int]$FrontendPort) {
    Assert-PortNumber $BackendPort 'BackendPort'
    Assert-PortNumber $FrontendPort 'FrontendPort'
    if ($BackendPort -eq $FrontendPort) { throw 'BackendPort and FrontendPort must differ.' }
    if (-not (Test-Path -LiteralPath $script:PythonPath -PathType Leaf)) { throw 'Backend virtualenv missing. Set up backend/.venv first.' }
    $pythonVersion = & $script:PythonPath -c 'import sys; print(sys.version_info.major,sys.version_info.minor,sep=chr(46))'
    if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]'3.12') { throw 'Python 3.12+ is required in backend/.venv.' }
    if (-not (Test-Path -LiteralPath $script:VitePath -PathType Leaf)) { throw 'Frontend dependencies missing. Run npm ci in frontend.' }
    if (-not (Test-Path -LiteralPath (Join-Path $script:BackendRoot 'alembic.ini') -PathType Leaf)) { throw 'alembic.ini is missing.' }
    $null = Get-NodePath
    Set-LauncherEnvironment $BackendPort
    $info = Get-RuntimeInfo
    if ($info.database_path -ne $info.api_database_path) { throw 'API and launcher resolve different SQLite database paths.' }
    return $info
}
