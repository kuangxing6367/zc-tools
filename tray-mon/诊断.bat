@echo off
rem Diagnostics: sample once and print what the monitor reads. No UI is shown.
cd /d "%~dp0"
echo ================================================================
echo  TrayMon diagnostics - one sample, no window
echo ================================================================
echo.
"python.exe" -u "tray_mon.py" --probe
echo.
echo ----------------------------------------------------------------
echo  Window / taskbar geometry (for troubleshooting docking):
echo ----------------------------------------------------------------
"python.exe" -u "tray_mon.py" --exit-after 3 --dump-rect
echo.
pause
