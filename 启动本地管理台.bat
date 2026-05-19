@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo.
echo 工厂加工费管理平台 - Windows 本地启动器
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1"

echo.
echo 管理台已退出。按任意键关闭窗口。
pause >nul
