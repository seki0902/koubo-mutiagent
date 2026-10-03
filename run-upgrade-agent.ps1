$ErrorActionPreference = 'Stop'
$upgradeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:AGENT_PORT = '8090'
$env:GO_CAPABILITY_BASE_URL = 'http://127.0.0.1:8909'
$upgradeRuntime = Join-Path $env:TEMP 'koubo-video-tool-agent-upgrade'
$env:DB_PATH = Join-Path $upgradeRuntime 'agent\app.sqlite'
# 与 Go 共用同一份设置文件：前端「Agent 模型设置」写的就是它，
# 由服务在每次对话前按修改时间重新读取，因此保存后无需重启。
$env:KOUBO_CONFIG_PATH = Join-Path $upgradeRuntime 'go\config.json'
$env:MODEL_API_KEY = if ($env:MODEL_API_KEY) { $env:MODEL_API_KEY } else { '' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $env:DB_PATH) | Out-Null
& python -m agent_service.app
exit $LASTEXITCODE
