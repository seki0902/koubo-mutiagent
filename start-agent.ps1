# Start both Agent services: Go gateway (8909) + Python Agent service (8090)
#
#   powershell -ExecutionPolicy Bypass -File .\start-agent.ps1
#
# This starts the Agent application only. The classic video tool is a separate
# application with its own launcher, port and settings file, and neither one
# needs the other to run.
#
# Each service runs in its own window so its log stays visible; closing that
# window stops that service. Use .\stop-agent.ps1 to stop both.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files with
# the system ANSI code page, so non-ASCII comments would be mis-decoded and
# break parsing.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtime = Join-Path $env:TEMP 'koubo-video-tool-agent-upgrade'
$exe = Join-Path $root 'koubo-video-tool-agent-upgrade.exe'
$venvPython = Join-Path $root 'agent_service\.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment interpreter not found: $venvPython (create agent_service\.venv and install requirements.txt first)."
}

New-Item -ItemType Directory -Force -Path (Join-Path $runtime 'go') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $runtime 'agent') | Out-Null

if (-not (Test-Path $exe)) {
    Write-Host 'Executable not found, building the Go service...' -ForegroundColor Yellow
    Push-Location $root
    try {
        & go build -o $exe .
        if ($LASTEXITCODE -ne 0) { throw 'go build failed' }
    } finally {
        Pop-Location
    }
}

# Commands for the child windows: joined with semicolons, no line continuation.
$goCommand = "`$env:KOUBO_PORT='8909'; `$env:KOUBO_DATA_DIR='$runtime\go'; `$env:KOUBO_APP_MODE='agent'; `$env:AGENT_SERVICE_BASE_URL='http://127.0.0.1:8090'; `$env:KOUBO_OPEN_BROWSER='0'; `$env:KOUBO_CREATE_DESKTOP_SHORTCUT='0'; Write-Host 'Agent gateway: http://127.0.0.1:8909' -ForegroundColor Cyan; & '$exe'"

$agentCommand = "Set-Location '$root'; `$env:GO_CAPABILITY_BASE_URL='http://127.0.0.1:8909'; `$env:DB_PATH='$runtime\agent\app.sqlite'; `$env:KOUBO_CONFIG_PATH='$runtime\go\config.json'; `$env:AGENT_PORT='8090'; Write-Host 'Agent service: http://127.0.0.1:8090' -ForegroundColor Cyan; & '$venvPython' -m uvicorn agent_service.app:app --host 127.0.0.1 --port 8090"

Write-Host 'Starting Go gateway (127.0.0.1:8909)...' -ForegroundColor Cyan
Start-Process -FilePath 'powershell' -ArgumentList '-NoExit', '-NoProfile', '-Command', $goCommand
Start-Sleep -Milliseconds 800
Write-Host 'Starting Python Agent service (127.0.0.1:8090)...' -ForegroundColor Cyan
Start-Process -FilePath 'powershell' -ArgumentList '-NoExit', '-NoProfile', '-Command', $agentCommand

function Wait-Port {
    param([int]$Port, [int]$Seconds = 40)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            return $true
        }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

$goUp = Wait-Port -Port 8909
$pyUp = Wait-Port -Port 8090

Write-Host ''
if ($goUp -and $pyUp) {
    Write-Host 'Both services are ready.' -ForegroundColor Green
    Write-Host 'Agent frontend: http://127.0.0.1:8909/agent.html'
    Write-Host 'Main page:      http://127.0.0.1:8909/'
} else {
    if (-not $goUp) { Write-Host 'Go gateway (8909) is not ready - check its window.' -ForegroundColor Red }
    if (-not $pyUp) { Write-Host 'Python service (8090) is not ready - check its window.' -ForegroundColor Red }
    Write-Host ''
    Write-Host 'If the Python window reports a missing module, install dependencies with:' -ForegroundColor Yellow
    Write-Host ('  & ''' + $venvPython + ''' -m pip install -r agent_service\requirements.txt') -ForegroundColor Yellow
    exit 1
}
