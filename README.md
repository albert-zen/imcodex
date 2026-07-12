# IMCodex

IM to Codex `app-server` thin bridge.

## Architecture

The codebase now follows a simple three-layer shape plus a thin wiring root:

- `imcodex.channels`
  Adapts QQ, Telegram, Feishu/Lark, experimental Tencent iLink Weixin, and the authenticated generic webhook.
- `imcodex.bridge`
  Owns IM-only bindings, slash commands, native request routing, and Codex event projection.
- `imcodex.appserver`
  Owns native Codex `app-server` integration for external Unix/TCP endpoints
  and the explicit bridge-child `stdio` compatibility target.

Supporting modules:

- `imcodex.composition`
  Builds the runtime graph and wires the three layers together. This is not a fourth business layer; it is the composition root.
- `imcodex.application`
  Exposes the FastAPI app and lifecycle hooks.
- `imcodex.runtime`
  Runs startup and shutdown for the assembled services.
- `imcodex.store`
  Persists only minimal bridge state: conversation bindings, visibility preferences, reply context, and pending native request routes.

The dependency direction is intentionally one-way:

- `channels` must not depend on `bridge` or `appserver`
- `bridge` may depend on `appserver`, but not on `channels`
- `appserver` must not depend on `bridge` or `channels`

This direction is enforced by architecture tests in [tests/test_architecture.py](tests/test_architecture.py).

## Docs

- [Startup and shutdown](docs/startup.md)
- [Channel setup and security](docs/channels.md)
- [Product behavior](docs/product-behavior-spec.md)
- [System constraints](docs/system-constraints-spec.md)

## Run

Install the standalone Codex CLI first and keep `codex` on `PATH`, or set
`IMCODEX_CODEX_BIN` to that standalone executable. The ChatGPT desktop bundle
is not treated as a substitute for this prerequisite: native daemon lifecycle
ultimately owns and updates its standalone managed Codex installation.

On macOS/Linux, the launcher ensures the native daemon is running and then
connects the bridge over its Unix control socket:

```bash
./scripts/start.sh
```

The equivalent explicit two-process workflow is:

```bash
python -m imcodex app-server start
export IMCODEX_APP_SERVER_URL=unix://
python -m imcodex
```

On native Windows, `scripts/start.ps1` starts or reuses the project's detached
TCP App Server and connects the bridge to `ws://127.0.0.1:8765`. The Python Unix
connector and native Codex daemon lifecycle are unavailable outside WSL;
`stdio://` remains an explicit bridge-child compatibility choice.

Helper scripts:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

An explicit `IMCODEX_APP_SERVER_URL` is connect-only on every platform. Running
`python -m imcodex` directly never starts an external App Server. Its unresolved
runtime default is `unix://` on Unix and `ws://127.0.0.1:8765` on native
Windows, so start the corresponding server first or use the platform launcher.

On Windows, double-click `scripts\start.cmd`, or run:

```cmd
scripts\start.cmd
```

On macOS, double-click `scripts/start.command` in Finder, or run:

```bash
open scripts/start.command
```

On Linux, run:

```bash
./scripts/start.sh
```

### Configuration console

After the bridge starts, open [http://127.0.0.1:8000/admin](http://127.0.0.1:8000/admin)
to manage its supported settings. If `IMCODEX_HTTP_PORT` is changed, use that
port while keeping the loopback host. The console is deliberately local-only:
it requires both a loopback client and a loopback `Host` header, and mutations
require a server-issued CSRF token. Binding the main HTTP service to
`0.0.0.0` does not make `/admin` remotely accessible.

The console preserves the native/bridge ownership boundary:

- Native Codex settings such as model, reasoning effort, personality, Fast
  mode, and permissions are read and written live through App Server. Codex
  remains their source of truth, and these changes do not require a bridge
  restart.
- Bridge and channel settings are written safely to the project's `.env`.
  They take effect after restarting IMCodex; the console does not pretend to
  hot-reload them.
- A value supplied by the process environment overrides `.env` and is shown as
  read-only, so the console cannot create a misleading lower-precedence value.
- Secret values are never returned to the browser. Existing secrets can only
  be preserved, replaced, or explicitly cleared.

See [Startup and shutdown](docs/startup.md#configuration-console) for the
operator details and security boundary.

### Native App Server daemon

On macOS and Linux, a recent standalone Codex CLI can own a long-lived local
App Server daemon. IMCodex provides a thin delegate for its lifecycle:

```bash
python -m imcodex app-server start
python -m imcodex app-server status
python -m imcodex app-server restart
python -m imcodex app-server stop
```

`start`, `restart`, and `stop` map directly to the same native daemon commands;
`status` maps to `codex app-server daemon version`. Native stdout, stderr, and
exit status are preserved, and IMCodex does not keep a PID or daemon manifest.
`IMCODEX_CODEX_BIN` selects the CLI used to issue the command; the native daemon
itself launches Codex from the standalone managed install reported by `status`.
If the command is missing, IMCodex fails with standalone-install guidance
instead of guessing a desktop application bundle path.

On macOS/Linux, connect the bridge to that independently owned daemon with:

```bash
export IMCODEX_APP_SERVER_URL=unix://
python -m imcodex
```

This workflow is capability-gated by `codex app-server daemon --help` and was
verified with `codex-cli 0.144.1`. Native daemon lifecycle management is
currently Unix-only. On Windows, use the existing independent TCP websocket
`core`, explicit `spawned-stdio`, or WSL path; there is no automatic fallback.

## Native-First State

`imcodex` now treats native Codex source code and native protocol behavior as
the source of truth when deciding where state should live and which layer
should own a feature.

That means:

- first check whether native Codex already implements the capability
- integrate with native behavior directly when it exists
- only persist bridge-owned state when native Codex does not expose the needed
  behavior and the IM bridge still must route or recover it

Native Codex remains the source of truth for:

- thread lifecycle
- turn lifecycle
- request identity
- model continuity
- reasoning effort
- Fast mode service tier
- permission and sandbox behavior

Persisted bridge state is intentionally small:

- `channel_id + conversation_id -> native thread_id`
- `bootstrap_cwd` before a native thread exists
- visibility preferences
- channel reply context
- pending native request routing

## App Server Target

Normal operation no longer has separate `dedicated-ws` and `shared-ws` modes.
From the bridge's perspective both were the same external, persistent App
Server. Configure the target directly:

- `unix://` or `unix:///absolute/path`: external Unix WebSocket, preferred locally
- `ws://` or `wss://`: external TCP WebSocket compatibility
- `stdio://`: explicit bridge-child compatibility target

External targets preserve native App Server state across bridge restarts and
use background reconnect. A restored connection is reported as `degraded`
when native thread rehydration cannot verify every binding. Native server
requests such as approvals are isolated from ordinary notification work, and
bounded dispatch overflow resets the connection so recovery can reconcile
native state explicitly. Approvals that cannot be delivered to the IM channel
within a bounded interval are explicitly rejected rather than leaving Codex
stuck, and a terminal result produced during a disconnect is recovered from the
native resume payload. `stdio://` lives and dies with the bridge and never
acts as an automatic fallback. Legacy `dedicated-ws` and `shared-ws` values are
accepted as external aliases, and `spawned-stdio` maps to `stdio://`; `auto` is
rejected because it silently changed which App Server owned a request. Native
Windows cannot use the Unix connector; the launcher therefore defaults to the
independent local TCP target. Configure `stdio://` only when bridge-child
lifecycle is intentional, or use WSL for the native Unix daemon.

WebSocket target URLs must not embed userinfo, query, or fragment credentials.
Use `IMCODEX_APP_SERVER_AUTH_TOKEN_FILE` (preferred) or
`IMCODEX_APP_SERVER_AUTH_TOKEN` so secrets never enter endpoint diagnostics or
restart snapshots.

## Channels

Built-in channel support now includes:

| Channel | Ingress | Scope | Status |
| --- | --- | --- | --- |
| QQ | Gateway websocket | Private and group `@bot` text | Stable |
| Telegram | Bot API long polling | Private, group, and forum-topic text | Stable |
| Feishu / Lark | Official Channel SDK websocket | Private, group, and topic text | Stable |
| Weixin | Tencent iLink long polling | Direct text only | Experimental |
| Generic webhook | HTTP | Trusted adapter injection | Loopback-only by default |

All remote IM adapters use stable platform user IDs for admission. An empty
allowlist denies every inbound user; `*` is an explicit opt-out, not a default.
Group adapters require an explicit mention by default.

Inspect and validate channel configuration without revealing credentials:

```powershell
python -m imcodex channels list
python -m imcodex channels doctor
```

Feishu/Lark uses an optional official SDK:

```powershell
pip install -e ".[feishu]"
```

See [Channel setup and security](docs/channels.md) for platform creation,
permissions, allowlist discovery, QR login, Windows notes, and troubleshooting.

## Inbound Webhook

`POST /api/channels/webhook/inbound`

The endpoint accepts unauthenticated requests only from loopback. To call it
through a remote gateway, set `IMCODEX_INBOUND_WEBHOOK_TOKEN` and send
`Authorization: Bearer <token>` over HTTPS.

Generic callers must use a dedicated channel ID such as `wecom-gateway`; the
built-in `qq`, `telegram`, `feishu`, and `weixin` IDs are reserved. Configure
`IMCODEX_OUTBOUND_URL` as well, because normal prompt results arrive
asynchronously and are POSTed back to that gateway. Remote callbacks also
require `IMCODEX_OUTBOUND_WEBHOOK_TOKEN`, which the gateway verifies as a
Bearer token. See
[Channel setup and security](docs/channels.md#generic-inbound-webhook) for the
full two-way payload, size limits, and idempotency behavior.

Example body:

```json
{
  "channel_id": "demo",
  "conversation_id": "conv-1",
  "user_id": "u1",
  "message_id": "m1",
  "text": "/cwd D:\\work\\alpha"
}
```

## Deployment Short Version

1. Install Python 3.13+ and `codex`
2. Copy `.env.example` to `.env`
3. Fill in the required settings
4. Run `pip install -e .` (or `pip install -e ".[feishu]"` for Feishu/Lark)
5. Run `pwsh -File .\scripts\doctor.ps1`
6. Start with `pwsh -File .\scripts\start.ps1`

Codex version requirement:

- Use `codex-cli 0.120.0` or newer.
- Older Codex builds can miss current thread-list compatibility and may fail against newer local Codex state.

## Environment

- `IMCODEX_DATA_DIR`: state directory, default `.imcodex`
- `IMCODEX_RUN_DIR`: observability and runtime snapshot directory, default `.imcodex-run`
- `IMCODEX_CODEX_BIN`: codex binary, default `codex`
- `IMCODEX_APP_SERVER_URL`: App Server target; accepts external
  `unix://`/`ws://`/`wss://` endpoints or explicit compatibility `stdio://`;
  defaults to `unix://` on Unix and `ws://127.0.0.1:8765` on native Windows
- `IMCODEX_APP_SERVER_EXPERIMENTAL_API`: opt into experimental native app-server capabilities, default `false`
- `IMCODEX_APP_SERVER_AUTH_TOKEN_FILE`: optional file containing the websocket bearer token
- `IMCODEX_APP_SERVER_AUTH_TOKEN`: optional websocket bearer token; takes precedence over the token file and is not written to launch snapshots
- `IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS`: websocket connection attempts, default `3`
- `IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS`: retries for native overload responses, default `3`
- `IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY`: initial retry delay in seconds, default `0.25`
- `IMCODEX_APP_SERVER_RETRY_MAX_DELAY`: maximum retry delay in seconds, default `2.0`
- `IMCODEX_APP_SERVER_RETRY_JITTER`: retry jitter fraction, default `0.25`
- `IMCODEX_APP_SERVER_CONNECT_TIMEOUT`: websocket open timeout in seconds, default `3.0`
- `IMCODEX_APP_SERVER_HEALTH_TIMEOUT`: TCP `/readyz`/`/healthz` probe timeout in seconds, default `1.0`; Unix sockets use the WebSocket handshake itself
- `IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY`: first background reconnect delay after an immediate retry fails, default `0.5`
- `IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY`: maximum background reconnect delay, default `30.0`
- `IMCODEX_APP_SERVER_RECONNECT_JITTER`: background reconnect jitter fraction, default `0.25`

Reconnect delays must be positive, the maximum must be at least the initial
delay, and jitter must be between `0` and `1`.

- `IMCODEX_CORE_MODE`: deprecated compatibility alias; `dedicated-ws` and
  `shared-ws` mean external, `spawned-stdio` means `stdio://`, and `auto` is rejected
- `IMCODEX_CORE_URL`: deprecated alias for `IMCODEX_APP_SERVER_URL`
- `IMCODEX_CORE_PORT`: deprecated launcher alias for a local external TCP target
- `IMCODEX_RESTART_EXECUTOR`: optional bridge restart command
- `IMCODEX_DEBUG_API_ENABLED`: enable debug HTTP routes, default `false`
- `IMCODEX_LOG_LEVEL`: Python logging level, default `INFO`
- `IMCODEX_HTTP_HOST`: HTTP bind host, default `0.0.0.0`
- `IMCODEX_HTTP_PORT`: HTTP bind port, default `8000`
- `IMCODEX_OUTBOUND_URL`: optional outbound webhook target
- `IMCODEX_OUTBOUND_WEBHOOK_TOKEN`: Bearer token used to authenticate remote outbound callbacks; never written to launch snapshots
- `IMCODEX_INBOUND_WEBHOOK_TOKEN`: bearer token required for non-loopback inbound webhook callers; never written to launch snapshots
- `IMCODEX_SERVICE_NAME`: client name sent to app-server, default `imcodex`
- `IMCODEX_QQ_ENABLED`: enable QQ bot adapter, default `false`
- `IMCODEX_QQ_APP_ID`: QQ bot AppID
- `IMCODEX_QQ_CLIENT_SECRET`: QQ bot AppSecret
- `IMCODEX_QQ_API_BASE`: QQ API base, default `https://api.sgroup.qq.com`
- `IMCODEX_QQ_MARKDOWN_ENABLED`: send QQ outbound messages as Markdown rich text with plain-text fallback, default `true`
- `IMCODEX_QQ_ALLOWED_USER_IDS`: comma-separated QQ sender openids; empty denies all
- `IMCODEX_TELEGRAM_ENABLED`: enable Telegram Bot API long polling, default `false`
- `IMCODEX_TELEGRAM_BOT_TOKEN_FILE`: preferred local file containing the Telegram bot token
- `IMCODEX_TELEGRAM_ALLOWED_USER_IDS`: comma-separated numeric Telegram user IDs; empty denies all
- `IMCODEX_FEISHU_ENABLED`: enable Feishu/Lark websocket ingress, default `false`
- `IMCODEX_FEISHU_DOMAIN`: `feishu` or `lark`
- `IMCODEX_FEISHU_ALLOWED_USER_IDS`: comma-separated sender open_ids; empty denies all
- `IMCODEX_WEIXIN_ENABLED`: enable experimental Tencent iLink direct messaging, default `false`
- `IMCODEX_WEIXIN_STATE_DIR`: private credential/cursor/context-token directory; defaults under `IMCODEX_DATA_DIR`
- `IMCODEX_PYTHON`: Python executable used by helper scripts, default `python`
