@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0oslab.ps1" %*
exit /b %ERRORLEVEL%
