@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage.ps1" -Action start %*
if errorlevel 1 echo Startup failed. Check the message above and data\launcher logs.
pause
