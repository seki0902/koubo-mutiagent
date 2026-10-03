@echo off
setlocal
set GOOS=windows
set GOARCH=amd64
set CGO_ENABLED=0
go build -ldflags="-s -w" -o koubo-video-tool-agent-upgrade.exe .
if errorlevel 1 exit /b %errorlevel%
echo Build complete: koubo-video-tool-agent-upgrade.exe
dir koubo-video-tool-agent-upgrade.exe
endlocal
