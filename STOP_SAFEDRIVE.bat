@echo off
cd /d "C:\SafeDrive-Edge-AI"
echo Stopping SafeDrive...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process ^| Where-Object { $_.CommandLine -like '*C:\SafeDrive-Edge-AI*' -or $_.CommandLine -like '*yolo_server.py*' -or $_.CommandLine -like '*launcher.py*' -or $_.CommandLine -like '*main.py*' } ^| ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }"
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1
echo SafeDrive stopped.
pause
