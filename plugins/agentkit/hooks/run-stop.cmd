@echo off
setlocal

set "REPO_ROOT=%CD%"

:find_repo
if exist "%REPO_ROOT%\scripts\agentkit.cmd" goto run_hook
for %%I in ("%REPO_ROOT%\..") do set "PARENT=%%~fI"
if /I "%PARENT%"=="%REPO_ROOT%" goto launcher_missing
set "REPO_ROOT=%PARENT%"
goto find_repo

:run_hook
call "%REPO_ROOT%\scripts\agentkit.cmd" codex-stop-hook --log "%REPO_ROOT%\.agentkit\codex-stop-hook.log"
exit /b %errorlevel%

:launcher_missing
>&2 echo AgentKit Stop hook could not find scripts\agentkit.cmd above "%CD%".
exit /b 127
