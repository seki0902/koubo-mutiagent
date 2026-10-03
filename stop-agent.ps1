# Stop both Agent services (only 8909 and 8090; the legacy 8899 is untouched)
#
#   powershell -ExecutionPolicy Bypass -File .\stop-agent.ps1
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files with
# the system ANSI code page, so non-ASCII comments would break parsing.

$ErrorActionPreference = 'Stop'

foreach ($port in 8909, 8090) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        Write-Host "$port is not listening" -ForegroundColor DarkGray
        continue
    }
    foreach ($connection in $connections) {
        # $pid is a read-only automatic variable in PowerShell, so use another name.
        $processId = $connection.OwningProcess
        $processName = 'unknown'
        try {
            $processName = (Get-Process -Id $processId -ErrorAction Stop).ProcessName
        } catch {
            continue
        }
        Write-Host "Stopping $processName (pid $processId) on $port" -ForegroundColor Yellow
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 1
foreach ($port in 8909, 8090) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "$port is still listening" -ForegroundColor Red
    } else {
        Write-Host "$port released" -ForegroundColor Green
    }
}
