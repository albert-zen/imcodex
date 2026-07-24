@echo off
setlocal

set "REPO_ROOT=%~dp0.."
set "PYTHON_BIN=%REPO_ROOT%\.venv\Scripts\python.exe"

if not exist "%PYTHON_BIN%" (
    echo IMCodex virtual environment is unavailable: %PYTHON_BIN% 1>&2
    exit /b 2
)

"%PYTHON_BIN%" -m imcodex channels send --current --bridge-root "%REPO_ROOT%" %*
exit /b %ERRORLEVEL%
