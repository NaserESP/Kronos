param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$kronosRoot = Split-Path -Parent $PSScriptRoot
$kronosUrl = 'http://127.0.0.1:7071'
$kronosRunning = $false
$kronosState = $null
try {
    $kronosState = Invoke-RestMethod -Uri "$kronosUrl/api/state" -TimeoutSec 2
} catch {}
if ($null -ne $kronosState) {
    if ($kronosState.model -ne 'Kronos-base' -or $kronosState.symbol -ne 'BTCUSDT') { throw 'Port 7071 is occupied by another application.' }
    $kronosRunning = $true
}
if (-not $kronosRunning) {
    $kronosLogDir = Join-Path $PSScriptRoot 'state'
    New-Item -ItemType Directory -Force -Path $kronosLogDir | Out-Null
    $kronosStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $kronosProcess = Start-Process -FilePath "$kronosRoot/.venv/Scripts/python.exe" -ArgumentList @("`"$PSScriptRoot/app.py`"",'--autostart') -WorkingDirectory $kronosRoot -WindowStyle Hidden -RedirectStandardOutput "$kronosLogDir/server-$kronosStamp.log" -RedirectStandardError "$kronosLogDir/error-$kronosStamp.log" -PassThru
    for ($kronosAttempt=0; $kronosAttempt -lt 40; $kronosAttempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $kronosState = Invoke-RestMethod -Uri "$kronosUrl/api/state" -TimeoutSec 2
            if ($kronosState.model -eq 'Kronos-base') { $kronosRunning=$true; break }
        } catch {}
        if ($kronosProcess.HasExited) { throw 'Dashboard failed to start. Check dashboard/state/error-*.log.' }
    }
    if (-not $kronosRunning) { throw 'Dashboard startup timed out. Check dashboard/state logs.' }
}
Write-Output "Kronos dashboard: $kronosUrl"
if (-not $NoBrowser) { Start-Process $kronosUrl }
