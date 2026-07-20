# Startup and Shutdown

This document covers the local operator path for starting and stopping `imcodex`.

## Start

From the repository root:

Normal operation connects the bridge to one external App Server target. The
explicit `stdio://` bridge-child target remains available for compatibility.

Install the standalone Codex CLI first and keep `codex` on `PATH`, or set
`IMCODEX_CODEX_BIN` to that standalone executable. IMCodex deliberately does
not guess a Codex/ChatGPT desktop bundle path: native daemon lifecycle depends
on the standalone managed installation it reports through `app-server status`.

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
delegates to `scripts/start.sh`. With no App Server target configured,
`start.sh` exports `IMCODEX_APP_SERVER_URL=unix://`, asks native Codex to start
or reuse its App Server daemon, and then starts the bridge. Native Windows
cannot use that Unix control socket or native daemon lifecycle, so `start.ps1`
starts or reuses the project's detached TCP App Server and passes
`ws://127.0.0.1:8765` through a launcher-private managed-target value. It
deliberately leaves the public `IMCODEX_APP_SERVER_URL` unset so an explicit URL
remains distinguishable and connect-only.

An explicit `IMCODEX_APP_SERVER_URL` is always connect-only: neither launcher
starts another App Server. Explicit legacy `IMCODEX_CORE_MODE`,
`IMCODEX_CORE_URL`, or `IMCODEX_CORE_PORT` values retain the old TCP core
launcher behavior for rollback. Running `python -m imcodex` directly also does
not manage an external App Server lifecycle. Its default target is `unix://` on
Unix and `ws://127.0.0.1:8765` on native Windows, so start the corresponding
server first or use the platform launcher.

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
IMCODEX_APP_SERVER_URL=unix://
# Legacy launcher-only controls:
IMCODEX_CORE_PORT=8765
IMCODEX_CORE_MODE=dedicated-ws
IMCODEX_CORE_START_TIMEOUT=30
IMCODEX_APP_SERVER_EXPERIMENTAL_API=0
IMCODEX_NATIVE_THREAD_TOOL_HOST=0
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

The App Server target variables are treated as one configuration group. Target
precedence is the entry shell, then values injected by conda activation, then
`.env`; values from those groups are never mixed into one target tuple.
`doctor.ps1` does not activate conda itself, so run it from inside the selected
environment when conda environment variables define the App Server target. On
macOS/Linux it also verifies the native `app-server daemon` capability required
by `start.sh`; native Windows checks only the `app-server` command used by its
detached TCP launcher. The HTTP port check uses the configured
`IMCODEX_HTTP_HOST` bind address rather than assuming loopback.

Run only one bridge process for a given `IMCODEX_DATA_DIR`. A normal managed
restart stops the old bridge before starting its replacement. Sharing one data
directory between overlapping bridge processes, even on different HTTP ports,
is unsupported because channel bindings and the terminal-delivery outbox have
one bridge owner.

### Configuration console

Once IMCodex is running on the default port, open:

```text
http://127.0.0.1:8000/admin
```

If `IMCODEX_HTTP_PORT` is different, substitute that port. Keep the browser URL
on `127.0.0.1` (or another loopback spelling); the administration route is not
a remote control endpoint. Access is accepted only when both the network peer
and the HTTP `Host` are loopback. This remains true when the main HTTP service
binds `IMCODEX_HTTP_HOST=0.0.0.0`, so do not put `/admin` behind a remote reverse
proxy. State-changing requests also require the console's server-issued CSRF
token and an allowed same-origin request.

The page contains two kinds of configuration with different owners:

- Native Codex settings are loaded from Codex and written immediately through
  the connected App Server. Codex remains authoritative for model, reasoning
  effort, personality, Fast mode, and permission behavior; the console does
  not keep a second copy of that state.
- Bridge and channel settings are saved atomically to the repository `.env`.
  The current process continues using its startup configuration, so restart
  IMCodex after a successful save. Comments and settings outside the console's
  managed schema are preserved.

Shell, service-manager, or other process-environment values have higher
precedence than `.env`. The console reports those overrides as read-only; change
them at their source and restart instead of trying to shadow them in `.env`.
Values that the platform launcher itself imported from this project's `.env`
remain editable as `.env` values.

The built-in `ops restart` path preserves genuine process-environment
overrides but deliberately releases values that came from `.env` (or from
defaults) before launching the replacement process. The replacement therefore
re-reads the current `.env`, including a changed HTTP port, instead of replaying
the stale resolved launch snapshot.

Run `ops restart` from an environment that still contains every externally
owned setting used to start the bridge. This includes `IMCODEX_*` settings and
execution context used by native Codex or HTTPS, such as `PATH`, `HOME`,
`CODEX_HOME`, `CODEX_*`, `OPENAI_*`, certificate variables, and proxy
variables. The launch snapshot records only the names of those settings, never
their values. If any are missing, or if the fully reconstructed Settings,
channel prerequisites, local App Server executable, token file, HTTP bind
address, or a newly selected HTTP port fail preflight validation, restart
aborts before stopping the current process. Runtimes constructed by
embedding `imcodex` with an explicit `Settings` object are marked
restart-unsupported unless the caller passes `settings_source="environment"`
to declare that the object came from `Settings.from_env()`.

Immediately before stopping, restart checks `/healthz` against the PID and
instance ID in the launch snapshot, so a stale snapshot cannot signal a reused
PID. After launching the replacement, it probes the configured bridge bind host
(mapping wildcard binds to the matching loopback address) at `/healthz` again.
Success requires the IMCodex bridge identity and the replacement process PID;
an unrelated listener on the same port is not accepted as a healthy restart.
Preflight failures retain a bounded, non-secret diagnostic instead of only an
exit code.

Credentials are write-only in the console. The API and page never receive an
existing secret value: leaving a secret untouched preserves it, entering a new
value replaces it, and an explicit clear action removes it. The `.env` update
serializes configuration-console writers and rejects stale revisions. Do not
edit the file with an unrelated external tool while a console save is in
progress, because ordinary editors do not participate in that lock.

`IMCODEX_APP_SERVER_EXPERIMENTAL_API` is disabled by default. Set it only when
intentionally testing upstream experimental app-server protocol behavior.

`IMCODEX_NATIVE_THREAD_TOOL_HOST=1` declares that IMCodex handles supported
Desktop-style thread-management tools on the selected App Server. The
platform launchers set it automatically when they start or reuse the project's
independent App Server. Explicit connect-only endpoints leave it disabled so a
Desktop or other host cannot race IMCodex on side-effecting tool calls; set it
manually only when that explicit endpoint has no other dynamic-tool host. Host
mode also opts the App Server client into the upstream experimental protocol
capability required by dynamic tools and paged thread history; it does not
enable unrelated IMCodex product features. Host selection belongs to the App
Server connection topology, not to individual threads: when enabled, IMCodex
resolves native-mappable tool calls for Desktop-, CLI-, and IMCodex-created
threads alike. It does not persist or infer per-thread tool ownership.

For websocket cores that require bearer auth, set
`IMCODEX_APP_SERVER_AUTH_TOKEN_FILE` to a local file containing the token, or
set `IMCODEX_APP_SERVER_AUTH_TOKEN` directly in the process environment. The
direct token takes precedence when both are set and is intentionally not written
to launch snapshots. Userinfo, query, and fragment credentials in the target
URL are rejected. Websocket connect failures and native overload responses
use bounded exponential retry with jitter; external TCP WebSocket targets also
probe derived `/readyz` then `/healthz` HTTP endpoints before reporting the App
Server as unavailable.

Initial startup stays bounded by `IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS` and
fails explicitly when the configured App Server is unavailable. After an
external connection has completed initialization once, an unexpected disconnect
starts an independent background recovery loop. The loop
retries until the bridge shuts down, with delay capped by
`IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY`; explicit `stdio://` does not use this
background loop, and `auto` is rejected. Shutdown cancels any pending retry.

A transport connection is not considered fully restored on its own. Each new
connection epoch reruns native `initialize`, permission defaults, and bound
thread rehydration before health reports `appserver.status=connected`. If one or
more native bindings fail or cannot be verified, the connection remains usable
but health reports `appserver.status=degraded` with `rehydration` totals instead
of claiming complete recovery. During recovery, `health.json` reports
`appserver.status=reconnecting` together with the current retry attempt and
delay. The App Server health object also reports `ready`, `ownership`,
`transport`, a credential-safe `endpoint`, `connection_epoch`, and whether
background reconnect and verified local image paths are enabled. The IM
`/status` command presents the same
connection facts and remains useful when a native thread read is temporarily
unavailable. Recovery does not wait for another IM message. Reconnect delays
must be positive, the maximum must be at least the initial delay, and jitter
must be between `0` and `1`.

JSON-RPC responses are handled on the socket read fast path. Native server
requests such as approvals use a separate bounded dispatcher so a slow ordinary
notification cannot starve them, while `serverRequest/resolved` stays on that
same ordered lane. IM channel delivery for a native request is bounded; failure
removes its local route and sends a JSON-RPC error so the turn does not hang. If
either dispatch queue fills, the bridge
records `appserver.dispatch.overflow`, resets that connection epoch, and lets
normal reconnect reconciliation recover from native state. WebSocket frame
size is not capped by the bridge because native `thread/resume` may return a
legitimate full thread in one response. Rehydration clears cached active-turn
authority before resume, reports an active thread without an active turn as
unverified, and projects a terminal turn result that completed while the
transport or bridge process was offline. Active-turn state remains runtime-only;
the persisted state contains only a terminal-delivery checkpoint and, after
projection, the pending outbound message. Terminal delivery uses a stable
thread/turn identity and retries from that outbox until the channel sink accepts
it or the service is intentionally closed. Outbox draining runs after every
connection becomes ready, including `stdio://`; only native thread rehydration
depends on using a persistent external App Server.

### Native Windows: independent TCP App Server + bridge

Because native Codex daemon lifecycle is currently Unix-only, the Windows
launcher keeps the same two-process ownership model with a detached local TCP
App Server. With no target configured, `scripts/start.ps1` starts or reuses it
on port `8765`, publishes the canonical websocket target through an internal
launcher-only value, and then starts the bridge. Reuse is accepted only when
the core manifest, PID, command, listener, and App Server `/readyz` probe match.
On native Windows the command check reads the actual listener owner's live argv
rather than trusting only the manifest or another process in the same tree; an
unrelated process occupying that port fails explicitly.
Only after that verification does the bridge treat the App Server as sharing
its filesystem, which enables native `localImage` inputs on the default Windows
topology. An explicitly configured canonical `ws://127.0.0.1:<port>` receives
the same capability only when it resolves to the same project-managed core and
passes the full manifest, listener-owner, live-command, and readiness checks;
mere connectivity is insufficient. Remote, TLS, aliased-host, and unrecorded
loopback targets remain text-only. The client clears the capability on disconnect and repeats the
manifest, process, listener, command, and health verification for each new
connection epoch before accepting another local image path.
Built-in bridge restart asks the running Windows process to shut down through a
loopback-only, instance-bound control request. Uvicorn then runs the normal
lifespan shutdown, flushing bridge state and closing channel/native resources;
the caller's restart timeout applies to graceful shutdown as well as replacement
health readiness, and the restart path never falls back to force-terminating the process. Wildcard
binds are reachable through loopback. If the bridge is bound only to a
non-loopback interface or is hosted by a third-party ASGI runner without the
built-in shutdown callback, restart fails closed and the operator must stop it
through that service manager.
The equivalent explicit workflow is:

```powershell
$env:IMCODEX_PYTHON="C:\ProgramData\miniconda3\envs\imcodex\python.exe"

& $env:IMCODEX_PYTHON -m imcodex core start --port 8765

# Leave IMCODEX_APP_SERVER_URL unset so the launcher verifies and reuses it.
pwsh -File .\scripts\start.ps1
```

Normal startup also probes the configured bridge bind before observability
initialization. A second launch, or any unrelated listener using the same HTTP
endpoint, fails with an explicit port-availability error instead of deleting
or replacing `.imcodex-run/current` while the first process still owns it.

After startup, check `.imcodex-run/current/health.json`:

- `status` should be `healthy`
- `http.listening` should be `true`
- `appserver.connected` should be `true`
- `appserver.mode` should be `external`
- `appserver.ownership` should be `external`
- `appserver.transport` should describe the selected Unix, TCP, or stdio transport
- `appserver.connection_epoch` should be at least `1`

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

Enabled remote channels report `inbound_access_ready` and the derived
`access_policy_mode` alongside transport connectivity. Empty restrictions are
healthy and use the platform-delivered scope. An intentional `none` policy is
reported as `deny_all` but also remains ready because the configured mute is
working as requested. Run `python -m imcodex channels doctor` in deployment
checks and after channel configuration changes; doctor validates credentials,
dependencies, and transport prerequisites rather than requiring an access list.

### External TCP WebSocket compatibility

If another process already owns a websocket Codex core, point the bridge at it
explicitly:

```powershell
$env:IMCODEX_PYTHON="C:\ProgramData\miniconda3\envs\imcodex\python.exe"
$env:IMCODEX_APP_SERVER_URL="ws://127.0.0.1:8765"
& $env:IMCODEX_PYTHON -m imcodex
```

The bridge treats this as the same external ownership shape as Unix. TCP
WebSocket remains an upstream experimental/unsupported compatibility carrier.

### Recommended: native local Unix control socket and daemon

On macOS or Linux, Codex can own an independently managed App Server daemon on
its native local control socket. Start it through the project entry point:

```bash
python -m imcodex app-server start
python -m imcodex app-server status
```

Then start only the bridge:

```bash
export IMCODEX_APP_SERVER_URL=unix://
python -m imcodex
```

Here `unix://` means
`$CODEX_HOME/app-server-control/app-server-control.sock` (or
`~/.codex/app-server-control/app-server-control.sock` when `CODEX_HOME` is not
set). Codex also accepts an explicit absolute path such as
`unix:///tmp/codex-app-server.sock`, or a path relative to the process working
directory such as `unix://run/codex.sock`. The suffix is a native file path, not
a URL path.

The project commands are deliberately thin delegates:

- `app-server start` -> `codex app-server daemon start`
- `app-server restart` -> `codex app-server daemon restart`
- `app-server stop` -> `codex app-server daemon stop`
- `app-server status` -> `codex app-server daemon version`

They use `IMCODEX_CODEX_BIN` to select the CLI issuing the command and preserve
native output and exit status. The daemon itself launches the standalone managed
Codex binary reported by `app-server status`; IMCodex does not track its PID or
write a second lifecycle manifest. Capability is detected through
`codex app-server daemon --help`, rather than a hard-coded minimum version. This
workflow was verified with `codex-cli 0.144.1`. A build or installation without
the daemon command reports its native error unchanged.

The connection carries standard WebSocket frames, so initialization, connection
epochs, native rehydration, and background reconnect are identical to persistent
TCP WebSocket connections. Unix sockets do not expose HTTP `/readyz` or
`/healthz`; a successful WebSocket Upgrade is the availability check. Native
Windows fails this bridge endpoint explicitly; use the default detached TCP
launcher, WSL, or explicit `stdio://` bridge-child compatibility there.

The upstream transport contract is documented in the
[Codex App Server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).

### Explicit bridge-child stdio compatibility

The platform helper scripts start the recommended independent App Server shape:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

Set the App Server target to `stdio://` to use the bridge-managed compatibility
path explicitly:

```powershell
$env:IMCODEX_APP_SERVER_URL="stdio://"
python -m imcodex
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

Stopping the bridge does not stop an independently managed native App Server.
On macOS/Linux, stop the native daemon explicitly when intended:

```bash
python -m imcodex app-server stop
```

On native Windows, the platform launcher owns the detached TCP compatibility
process instead. Inspect and stop that project-owned process with:

```powershell
python -m imcodex core status
python -m imcodex core stop
```

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

## Target Summary

- `unix://`: recommended external native App Server and Unix default
- `ws://` or `wss://`: external TCP WebSocket; the native Windows launcher
  defaults to `ws://127.0.0.1:8765` and owns that detached local process
- `stdio://`: explicit bridge-child compatibility target

Legacy `dedicated-ws` and `shared-ws` normalize to external ownership;
`spawned-stdio` normalizes to `stdio://`. Connection failure never switches the
configured target.
