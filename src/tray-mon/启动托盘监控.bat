@echo off
rem Launch the taskbar performance monitor without a console window.
rem Requires the bundled python.exe / pythonw.exe next to this file.
cd /d "%~dp0"
if not exist "pythonw.exe" (
  echo.
  echo   pythonw.exe not found next to this .bat
  echo   Please extract the WHOLE zip and run this file inside that folder.
  echo.
  pause
  exit /b 1
)
start "" "pythonw.exe" "tray_mon.py"
exit /b 0
