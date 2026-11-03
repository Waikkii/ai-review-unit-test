@echo off
title AI Review - LAN
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage.ps1" -Action rebuild -BindAddress 0.0.0.0 %*
set "startupExit=%errorlevel%"
if not "%startupExit%"=="0" echo Startup failed. Check the message above and data\launcher logs.
pause
exit /b %startupExit%
