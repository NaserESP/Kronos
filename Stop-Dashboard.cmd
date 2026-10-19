@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard\Stop-Dashboard.ps1"
if errorlevel 1 pause
