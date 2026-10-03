@echo off
chcp 65001 >nul
title 停止 Koubo Multi-Agent
cd /d "%~dp0"

echo 正在停止 Agent 应用的两个服务（8909 / 8090）...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "foreach ($port in 8909, 8090) { $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $c) { Write-Host ('端口 ' + $port + ' 没有在跑'); continue }; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess) -ErrorAction SilentlyContinue; if ($null -eq $p) { continue }; Write-Host ('停止端口 ' + $port + '（PID ' + $p.ProcessId + ' ' + $p.Name + '）'); $parent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.ParentProcessId) -ErrorAction SilentlyContinue; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; if ($parent -and $parent.Name -eq 'powershell.exe') { Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue } }"

echo.
pause
