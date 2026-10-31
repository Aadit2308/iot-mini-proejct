@echo off
rem Smart Irrigation launcher - double-click for a menu, or:
rem   launch.bat python ^| native ^| cooja [scenario]   /   launch.bat stop ^| status ^| setup ^| test
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
