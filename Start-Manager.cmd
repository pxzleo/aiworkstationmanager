@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Manager.ps1" %*
if errorlevel 1 exit /b %errorlevel%
