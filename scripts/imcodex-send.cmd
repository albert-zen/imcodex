@echo off
setlocal

set "REPO_ROOT=%~dp0.."

if defined IMCODEX_PYTHON (
    set "PYTHON_BIN=%IMCODEX_PYTHON%"
) else if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_BIN=%REPO_ROOT%\.venv\Scripts\python.exe"
) else if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" (
    set "PYTHON_BIN=%CONDA_PREFIX%\python.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo IMCodex requires Python, but no interpreter was found. 1>&2
        exit /b 127
    )
    set "PYTHON_BIN=python"
)

"%PYTHON_BIN%" -m imcodex channels send --current --bridge-root "%REPO_ROOT%" %*
exit /b %ERRORLEVEL%
