@echo off
rem Apply system clock sync. Requests UAC elevation when needed.
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 goto elevate
"python.exe" ntp_sync.py --apply --log-file sync.log %*
echo.
pause
goto :eof

:elevate
echo Requesting administrator privileges (UAC)...
"python.exe" ntp_sync.py --apply --elevate --log-file sync.log %*
if errorlevel 1 pause
goto :eof
