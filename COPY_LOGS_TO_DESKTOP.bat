@echo off
set "DST=%USERPROFILE%\Desktop\SafeDrive_RUNTIME_LOGS"
if not exist "%DST%" mkdir "%DST%"
if exist "C:\SafeDrive-Edge-AI\logs" xcopy "C:\SafeDrive-Edge-AI\logs\*" "%DST%\" /E /I /Y
echo Logs copied to %DST%
pause
