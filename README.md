# IMCodex

IM to Codex `app-server` thin bridge.

## Architecture

The codebase now follows a simple three-layer shape plus a thin wiring root:

- `imcodex.channels`
  Adapts QQ, Telegram, Feishu/Lark, experimental Tencent iLink Weixin, and the authenticated generic webhook.
- `imcodex.bridge`
  Owns IM-only bindings, slash commands, native request routing, and Codex event projection.
- `imcodex.appserver`
  Owns native Codex `app-server` integration across the supported runtime modes:
  dedicated websocket cores, externally managed websocket cores, and
  bridge-managed local `stdio` supervision.

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

```powershell
python -m imcodex
```

Helper scripts:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

On Windows, double-click `scripts\start.cmd`, or run:

```cmd
scripts\start.cmd
```

On macOS, double-click `scripts/start.command` in Finder, or run:

```bash
open scripts/start.command
```

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

## Runtime Modes

`imcodex` intentionally supports more than one Codex-core runtime shape.

- `dedicated-ws`
  Recommended for day-to-day IM use. Run a long-lived Codex core separately and
  point the bridge at it.
- `shared-ws`
  Supported when another process already manages a websocket Codex core and the
  bridge should attach to it.
- `spawned-stdio`
  Supported as a bridge-managed compatibility mode for local checks, fallback,
  and some test paths.

We do **not** currently treat dedicated core as the only valid mode. The
bridge must stay clear about which mode it is using, and changes should not
silently remove support for the other documented paths without an explicit
follow-up decision.

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
- `IMCODEX_APP_SERVER_URL`: optional websocket URL for a shared Codex app-server
- `IMCODEX_APP_SERVER_EXPERIMENTAL_API`: opt into experimental native app-server capabilities, default `false`
- `IMCODEX_APP_SERVER_AUTH_TOKEN_FILE`: optional file containing the websocket bearer token
- `IMCODEX_APP_SERVER_AUTH_TOKEN`: optional websocket bearer token; takes precedence over the token file and is not written to launch snapshots
- `IMCODEX_APP_SERVER_CONNECT_MAX_ATTEMPTS`: websocket connection attempts, default `3`
- `IMCODEX_APP_SERVER_REQUEST_MAX_ATTEMPTS`: retries for native overload responses, default `3`
- `IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY`: initial retry delay in seconds, default `0.25`
- `IMCODEX_APP_SERVER_RETRY_MAX_DELAY`: maximum retry delay in seconds, default `2.0`
- `IMCODEX_APP_SERVER_RETRY_JITTER`: retry jitter fraction, default `0.25`
- `IMCODEX_APP_SERVER_CONNECT_TIMEOUT`: websocket open timeout in seconds, default `3.0`
- `IMCODEX_APP_SERVER_HEALTH_TIMEOUT`: `/readyz`/`/healthz` probe timeout in seconds, default `1.0`
- `IMCODEX_CORE_MODE`: Codex core mode, default `spawned-stdio`
- `IMCODEX_CORE_URL`: optional dedicated Codex core websocket URL
- `IMCODEX_RESTART_EXECUTOR`: optional bridge restart command
- `IMCODEX_DEBUG_API_ENABLED`: enable debug HTTP routes, default `false`
- `IMCODEX_LOG_LEVEL`: Python logging level, default `INFO`
- `IMCODEX_HTTP_HOST`: HTTP bind host, default `0.0.0.0`
- `IMCODEX_HTTP_PORT`: HTTP bind port, default `8000`
- `IMCODEX_OUTBOUND_URL`: optional outbound webhook target
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
