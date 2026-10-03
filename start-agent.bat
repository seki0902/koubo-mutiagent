@echo off
chcp 65001 >nul
title Koubo Multi-Agent（Agent 应用）
cd /d "%~dp0"

echo ============================================
echo   Koubo Multi-Agent 启动
echo   Go 网关 8909  +  Python 服务 8090
echo ============================================
echo.

rem 已经在跑就不再启动第二份（否则两个进程会抢同一个端口）
powershell -NoProfile -Command "if ((Get-NetTCPConnection -State Listen -LocalPort 8909 -ErrorAction SilentlyContinue) -and (Get-NetTCPConnection -State Listen -LocalPort 8090 -ErrorAction SilentlyContinue)) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-agent.ps1"
) else (
  echo 服务已经在运行，直接打开页面。
)

echo.
echo 打开 http://127.0.0.1:8909/
start "" http://127.0.0.1:8909/

echo.
echo 服务在各自的控制台窗口里运行，这个窗口可以关掉。
echo 停止服务：stop-agent.bat
echo.
pause
