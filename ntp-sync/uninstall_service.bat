@echo off
rem Stop and remove the sync service (must run as administrator).
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo   This file must be run as administrator.
  echo   Right-click uninstall_service.bat and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)
"python.exe" ntpsvc.py uninstall
echo.
pause
