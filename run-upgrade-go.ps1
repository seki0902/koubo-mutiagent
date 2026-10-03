$ErrorActionPreference = 'Stop'
$upgradeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:KOUBO_PORT = '8909'
$upgradeRuntime = Join-Path $env:TEMP 'koubo-video-tool-agent-upgrade'
$env:KOUBO_DATA_DIR = Join-Path $upgradeRuntime 'go'
$env:AGENT_SERVICE_BASE_URL = 'http://127.0.0.1:8090'
$env:KOUBO_OPEN_BROWSER = '0'
$env:KOUBO_CREATE_DESKTOP_SHORTCUT = '0'
New-Item -ItemType Directory -Force -Path $env:KOUBO_DATA_DIR | Out-Null
& go run .
exit $LASTEXITCODE
