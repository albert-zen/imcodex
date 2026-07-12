@echo off
setlocal

set "REPO_ROOT=%~dp0.."

if defined AGENTKIT_PYTHON (
    set "PYTHON=%AGENTKIT_PYTHON%"
) else if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo AgentKit requires Python, but no interpreter was found. 1>&2
        exit /b 127
    )
    set "PYTHON=python"
)

"%PYTHON%" -P -c "import agentkit.__main__" >nul 2>nul
if errorlevel 1 goto agentkit_missing

"%PYTHON%" -P -m agentkit --repo "%REPO_ROOT%" %*
exit /b %errorlevel%

:agentkit_missing
>&2 echo AgentKit is not installed for "%PYTHON%".
>&2 echo From "%REPO_ROOT%", prefer:
>&2 echo   uv pip install --python "%PYTHON%" -e ".[agentkit]"
>&2 echo This works even when the selected virtual environment has no pip.
>&2 echo Without uv, bootstrap pip if available, then install:
>&2 echo   "%PYTHON%" -m ensurepip --upgrade
>&2 echo   "%PYTHON%" -m pip install -e ".[agentkit]"
exit /b 127
