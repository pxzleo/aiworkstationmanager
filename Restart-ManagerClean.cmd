@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "D:\AIWork\4090manager\Restart-ManagerClean.ps1" -Pause
exit /b %errorlevel%
