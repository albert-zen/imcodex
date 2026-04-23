# Startup and Shutdown

This document covers the local operator path for starting and stopping `imcodex`.

## Start

From the repository root:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

The helper script runs:

```powershell
python -m imcodex
```

If you use a dedicated Python environment, activate it first or set `IMCODEX_PYTHON`:

```powershell
$env:IMCODEX_PYTHON="C:\ProgramData\miniconda3\envs\imcodex\python.exe"
pwsh -File .\scripts\start.ps1
```

## Stop

If the bridge is running in the current terminal, press `Ctrl+C`.

If it is running in the background, find the process that owns the configured HTTP port:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object LocalAddress,LocalPort,State,OwningProcess
```

Then inspect the command line before stopping it:

```powershell
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" |
  Format-List ProcessId,ExecutablePath,CommandLine
```

Stop only the matching `imcodex` process:

```powershell
Stop-Process -Id <PID>
```

Use `-Force` only when the normal stop does not exit:

```powershell
Stop-Process -Id <PID> -Force
```

## Change Port

The default HTTP bind is `0.0.0.0:8000`. To avoid conflicts with another local app, set `IMCODEX_HTTP_PORT` before starting:

```powershell
$env:IMCODEX_HTTP_PORT="8010"
pwsh -File .\scripts\start.ps1
```

For a persistent change, put it in `.env`:

```env
IMCODEX_HTTP_PORT=8010
```

## Port Conflicts

Two listeners can look confusing when one app binds `0.0.0.0:<port>` and another binds `127.0.0.1:<port>`. Always check both the listener address and the owning process command line before stopping anything.

If `http://127.0.0.1:8000` does not reach `imcodex`, another local process may own the loopback listener. Change `IMCODEX_HTTP_PORT` or stop the other process intentionally.

## Runtime Files

By default:

- Bridge state lives under `.imcodex`
- Runtime and observability snapshots live under `.imcodex-run`
- The current launch snapshot is written under `.imcodex-run/current/launch.json`
