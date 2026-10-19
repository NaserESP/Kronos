$ErrorActionPreference = 'Stop'
$kronosUrl = 'http://127.0.0.1:7071'
$kronosState = Invoke-RestMethod -Uri "$kronosUrl/api/state" -TimeoutSec 3
if ($kronosState.model -ne 'Kronos-base' -or $kronosState.symbol -ne 'BTCUSDT') { throw 'Unexpected application on port 7071.' }
Invoke-RestMethod -Uri "$kronosUrl/api/control" -Method Post -ContentType 'application/json' -Body '{"action":"shutdown"}' -TimeoutSec 3 | Out-Null
Write-Output 'Kronos dashboard stopped.'
