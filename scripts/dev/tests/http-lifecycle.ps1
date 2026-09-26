# Real Windows HTTP/venv fixture; default ASGI fixture has no application/DB imports.
# -IsolatedBackend uses only /health with a temporary SQLite file, never Alembic.
param([switch]$TraceOnly, [switch]$Uvicorn, [switch]$IsolatedBackend)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')
$fixtureDirectory = Join-Path ([IO.Path]::GetTempPath()) ('catalog-http-fixture-' + [guid]::NewGuid().ToString('n'))
New-Item -ItemType Directory -Path $fixtureDirectory | Out-Null
$reservation = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
$reservation.Start()
$fixturePort = $reservation.LocalEndpoint.Port
$reservation.Stop()
$service = [pscustomobject]@{
    id='backend'; name='Isolated HTTP fixture'; kind='http'; executable=$script:PythonPath
    directory=$fixtureDirectory; port=$fixturePort; marker='catalog-http-fixture'
    arguments=@('-c',('"import http.server; http.server.HTTPServer((''127.0.0.1'', {0}), http.server.SimpleHTTPRequestHandler).serve_forever()"' -f $fixturePort),'catalog-http-fixture')
    url="http://127.0.0.1:$fixturePort/"
}
if ($Uvicorn) {
    $service.arguments = @('-m','uvicorn','http_fixture:app','--app-dir',('"{0}"' -f (Join-Path $PSScriptRoot 'fixtures')),'--host','127.0.0.1','--port',"$fixturePort")
    $service.marker = 'http_fixture:app'
    $service.url = "http://127.0.0.1:$fixturePort/health"
}
$priorDatabaseUrl = $env:DATABASE_URL
if ($IsolatedBackend) {
    $env:DATABASE_URL = 'sqlite:///' + (Join-Path $fixtureDirectory 'isolated.db').Replace('\','/')
    $service.directory = $script:BackendRoot
    $service.arguments = @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$fixturePort")
    $service.marker = 'app.main:app'
    $service.url = "http://127.0.0.1:$fixturePort/health"
}
$record = $null
$tracked = @()
try {
    $record = Start-ManifestService $service $fixtureDirectory
    if ($TraceOnly) {
        for ($sample=0; $sample -lt 5; $sample++) {
            $identity=Get-ProcessOwnership $record
            $http=Test-HttpReady $record.url
            $listener=Get-PortOccupant $fixturePort
            $related=$false
            if ($null -ne $listener) { $related=Test-ListenerRelated $record $listener.pid }
            [pscustomobject]@{
                powershell=$PSVersionTable.PSVersion.ToString(); sample=$sample
                launch_pid=$record.pid; launch_path=$record.executable; launch_start=$record.start_time_utc
                ownership=$identity.status; reason=$identity.reason; health=$http; listener=$listener; related=$related
                lineage=$record.lineage
            } | ConvertTo-Json -Compress -Depth 4
            $parents=Get-ProcessParents
            Get-OwnedDescendants $parents $record.pid (ConvertTo-ProcessStartUtc $record.start_time_utc) | ConvertTo-Json -Compress
            Start-Sleep -Milliseconds 400
        }
    }
    Wait-ServiceReady $record 25
    if (-not (Test-OwnedService $record)) { throw 'HTTP fixture not recognized as owned.' }
    $tracked = @($record) + @(Get-OwnedDescendants (Get-ProcessParents) $record.pid (ConvertTo-ProcessStartUtc $record.start_time_utc))
    Write-Output 'PASS real isolated HTTP readiness and ownership.'
} finally {
    try {
        if ($null -ne $record) { Stop-OwnedService $record }
        foreach ($item in $tracked) {
            $fresh = Get-Process -Id $item.pid -ErrorAction SilentlyContinue
            if ($null -ne $fresh -and $fresh.StartTime.ToUniversalTime() -eq (ConvertTo-ProcessStartUtc $item.start_time_utc)) {
                throw "Owned HTTP fixture PID $($item.pid) did not stop."
            }
        }
    } finally { $env:DATABASE_URL = $priorDatabaseUrl }
    Write-Output "Isolated fixture logs: $fixtureDirectory"
}
