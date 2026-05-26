@echo off
setlocal

cd /d "%~dp0\.."

where pwsh >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
) else (
    powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
)

set "IMCODEX_EXIT_CODE=%ERRORLEVEL%"
if not "%IMCODEX_NO_PAUSE%"=="1" pause
exit /b %IMCODEX_EXIT_CODE%
