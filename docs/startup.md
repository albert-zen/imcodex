# Startup and Shutdown

This document covers the local operator path for starting and stopping `imcodex`.

## Start

From the repository root:

`imcodex` currently supports multiple runtime shapes. Dedicated core is the
recommended day-to-day path, but it is not the only supported mode.

### Recommended: dedicated core + bridge

For day-to-day IM use, prefer running a long-lived Codex core separately and
then starting the IM bridge against it. This keeps the native agent core alive
across bridge restarts.

```powershell
$env:IMCODEX_PYTHON="C:\ProgramData\miniconda3\envs\imcodex\python.exe"

& $env:IMCODEX_PYTHON -m imcodex core start --port 8765

$env:IMCODEX_CORE_MODE="dedicated-ws"
$env:IMCODEX_CORE_URL="ws://127.0.0.1:8765"
pwsh -File .\scripts\start.ps1
```

After startup, check `.imcodex-run/current/health.json`:

- `status` should be `healthy`
- `http.listening` should be `true`
- `appserver.connected` should be `true`
- `appserver.mode` should be `dedicated-ws`

If `appserver.mode` reports `shared-ws`, the bridge is attached to an
externally managed websocket core rather than the dedicated-core path.

Protocol troubleshooting data is written under `.imcodex-run/current/`:

- `bridge.log`
- `events.jsonl`
- `health.json`

`events.jsonl` includes summarized app-server messages sent and received by the
bridge. The summaries include transport shape, method names, request ids,
thread and turn ids, and short previews for diagnostic fields, but they avoid
recording full native payloads.

### Supported: externally managed websocket core

If another process already owns a websocket Codex core, point the bridge at it
explicitly:

```powershell
$env:IMCODEX_PYTHON="C:\ProgramData\miniconda3\envs\imcodex\python.exe"
$env:IMCODEX_CORE_MODE="shared-ws"
$env:IMCODEX_APP_SERVER_URL="ws://127.0.0.1:8765"
pwsh -File .\scripts\start.ps1
```

Use this mode when the websocket server lifecycle is not owned by `imcodex`
itself.

### Supported: bridge-managed core

The helper script can still start the bridge by itself:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

The helper script runs:

```powershell
python -m imcodex
```

Without `IMCODEX_CORE_MODE=dedicated-ws` and `IMCODEX_CORE_URL`, this path may
fall back to a bridge-managed `stdio` Codex app-server. It remains useful for
quick local checks, but it is not the preferred long-running IM setup.

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

## Mode Summary

- `dedicated-ws`: recommended long-lived IM setup
- `shared-ws`: supported attach-to-existing websocket setup
- `spawned-stdio`: supported bridge-managed compatibility setup

When changing startup behavior, update this document and keep the supported
modes explicit. Do not silently collapse multiple runtime modes into one.
