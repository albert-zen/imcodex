# Startup and Shutdown

This document covers the local operator path for starting and stopping `imcodex`.

## Start

From the repository root:

`imcodex` currently supports multiple runtime shapes. Dedicated core is the
recommended day-to-day path, but it is not the only supported mode.

### Platform quick start

On Windows, double-click `scripts\start.cmd`, or run this from the repository
root:

```cmd
scripts\start.cmd
```

On macOS, double-click `scripts/start.command` in Finder to open Terminal and
start the project, or run this from the repository root:

```bash
open scripts/start.command
```

The Windows launcher delegates to `scripts/start.ps1`; the macOS launcher
delegates to `scripts/start.sh`. By default, both start or reuse a dedicated
Codex core on `ws://127.0.0.1:8765`, export `IMCODEX_CORE_MODE=dedicated-ws`,
and then start the bridge.

If `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on macOS/Linux
exists, the launcher uses it automatically. Set `IMCODEX_PYTHON` only when you
want to override that interpreter.

Channel creation, credentials, stable-ID admission, and Windows-specific setup
are documented in [Channel Setup and Security](channels.md). Before starting an
enabled channel, run:

```powershell
python -m imcodex channels doctor
```

Optional environment controls:

```env
IMCODEX_CONDA_ENV=imcodex
IMCODEX_PYTHON=/path/to/python
IMCODEX_CORE_PORT=8765
IMCODEX_CORE_MODE=dedicated-ws
IMCODEX_CORE_START_TIMEOUT=30
IMCODEX_APP_SERVER_EXPERIMENTAL_API=0
IMCODEX_APP_SERVER_AUTH_TOKEN_FILE=.imcodex-appserver-token
IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS=3
IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS=3
IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY=0.25
IMCODEX_APP_SERVER_RETRY_MAX_DELAY=2.0
IMCODEX_APP_SERVER_RETRY_JITTER=0.25
IMCODEX_APP_SERVER_CONNECT_TIMEOUT=3.0
IMCODEX_APP_SERVER_HEALTH_TIMEOUT=1.0
IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY=0.5
IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY=30.0
IMCODEX_APP_SERVER_RECONNECT_JITTER=0.25
```

Values from the shell take precedence over `.env`. If `IMCODEX_CONDA_ENV` is
set, the launcher activates that conda environment before resolving
`IMCODEX_PYTHON`. On Windows, `scripts\start.cmd` keeps the terminal open after
the bridge exits; set `IMCODEX_NO_PAUSE=1` in the shell before running
`scripts\start.cmd` to skip that pause.

`IMCODEX_APP_SERVER_EXPERIMENTAL_API` is disabled by default. Set it only when
intentionally testing upstream experimental app-server protocol behavior.

For websocket cores that require bearer auth, set
`IMCODEX_APP_SERVER_AUTH_TOKEN_FILE` to a local file containing the token, or
set `IMCODEX_APP_SERVER_AUTH_TOKEN` directly in the process environment. The
direct token takes precedence when both are set and is intentionally not written
to launch snapshots. Websocket connect failures and native overload responses
use bounded exponential retry with jitter; dedicated/shared websocket modes also
probe derived `/readyz` then `/healthz` HTTP endpoints before reporting the core
as unavailable.

Initial startup stays bounded by `IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS` and
fails explicitly when the configured core is unavailable. After a
`dedicated-ws` or `shared-ws` connection has completed initialization once, an
unexpected disconnect starts an independent background recovery loop. The loop
retries until the bridge shuts down, with delay capped by
`IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY`; `spawned-stdio` and `auto` do not use
this background loop. Shutdown cancels any pending retry.

A transport connection is not considered fully restored on its own. Each new
connection epoch reruns native `initialize`, permission defaults, and bound
thread rehydration before health reports `appserver.status=connected`. During
recovery, `health.json` reports `appserver.status=reconnecting` together with
the current retry attempt and delay. Recovery does not wait for another IM
message. Reconnect delays must be positive, the maximum must be at least the
initial delay, and jitter must be between `0` and `1`.

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

Reconnect history is recorded as `appserver.reconnect.scheduled`,
`appserver.reconnect.failed`, and `appserver.reconnect.succeeded` events with
attempt, delay, error type, and restored connection epoch where applicable.

Managed IM channels reconnect in the background. QQ and Feishu use websocket
connections; Telegram and experimental Weixin use cancellable long polling.
If a platform endpoint is temporarily unreachable, the bridge keeps the
HTTP/app-server path available while channel health reports the retry. Check
`.imcodex-run/current/health.json` under `channels.<channel-id>` for status,
retry delay, and the latest connection error type.

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

#### Native local Unix control socket (connect only)

On macOS or Linux, an independently managed Codex App Server can expose its
native local control socket. Start the App Server in its own terminal or service:

```bash
codex app-server --listen unix://
```

Then start only the bridge in another terminal:

```bash
export IMCODEX_CORE_MODE=shared-ws
export IMCODEX_APP_SERVER_URL=unix://
./scripts/start.sh
```

Here `unix://` means
`$CODEX_HOME/app-server-control/app-server-control.sock` (or
`~/.codex/app-server-control/app-server-control.sock` when `CODEX_HOME` is not
set). Codex also accepts an explicit absolute path such as
`unix:///tmp/codex-app-server.sock`, or a path relative to the process working
directory such as `unix://run/codex.sock`. The suffix is a native file path, not
a URL path.

This release connects to that socket but does not start or stop its App Server.
The connection still carries standard WebSocket frames, so initialization,
connection epochs, native rehydration, and background reconnect are identical
to persistent TCP WebSocket connections. Unix sockets do not expose HTTP
`/readyz` or `/healthz`; a successful WebSocket Upgrade is the availability
check. Native Windows fails this endpoint explicitly; use WSL, an explicit
`ws://` endpoint, or `spawned-stdio` there.

The upstream transport contract is documented in the
[Codex App Server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).

### Supported: bridge-managed core

The helper script can still start the bridge by itself:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

Set `IMCODEX_CORE_MODE=spawned-stdio` to use the bridge-managed stdio path:

```powershell
$env:IMCODEX_CORE_MODE="spawned-stdio"
pwsh -File .\scripts\start.ps1
```

This remains useful for quick local checks, but it is not the preferred
long-running IM setup.

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
