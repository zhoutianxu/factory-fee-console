@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo.
echo Factory Fee Console - Windows Launcher
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1"

echo.
echo Console stopped. Press any key to close this window.
pause >nul
