@echo off
rem Check only - reports the offset, NEVER changes the system clock.
cd /d "%~dp0"
"python.exe" ntp_sync.py %*
echo.
pause
