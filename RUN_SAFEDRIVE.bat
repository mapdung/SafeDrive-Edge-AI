@echo off
setlocal
cd /d "C:\SafeDrive-Edge-AI"
set "PYTHONHOME="
set "PYTHONPATH="
if not exist "logs" mkdir "logs"
call ".venv\Scripts\activate.bat"
call "scripts\start_all.bat" >> "logs\run_safedrive.log" 2>&1
