# IMCodex

IM to Codex `app-server` thin bridge.

## Architecture

The codebase now follows a simple three-layer shape plus a thin wiring root:

- `imcodex.channels`
  Adapts concrete IM and transport surfaces such as QQ and the generic webhook API.
- `imcodex.bridge`
  Owns IM-only bindings, slash commands, native request routing, and Codex event projection.
- `imcodex.appserver`
  Owns native Codex `app-server` integration, preferring a shared websocket server when available and falling back to local `stdio` supervision for thread/turn operations.

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

## Native-First State

`imcodex` now treats native Codex as the source of truth for:

- thread lifecycle
- turn lifecycle
- request identity
- model continuity
- permission and sandbox behavior

Persisted bridge state is intentionally small:

- `channel_id + conversation_id -> native thread_id`
- `bootstrap_cwd` before a native thread exists
- visibility preferences
- channel reply context
- pending native request routing

## QQ Bot

Set these environment variables to enable the built-in QQ adapter:

- `IMCODEX_QQ_ENABLED=1`
- `IMCODEX_QQ_APP_ID=<your AppID>`
- `IMCODEX_QQ_CLIENT_SECRET=<your AppSecret>`
- `IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com`

The adapter currently supports:

- `C2C_MESSAGE_CREATE` for private chat
- `GROUP_AT_MESSAGE_CREATE` for group `@bot` chat

QQ inbound messages are mapped to internal conversation ids like `c2c:<openid>` and `group:<group_openid>`, so Codex thread routing and async completion messages can flow back through the QQ API.

## Inbound Webhook

`POST /api/channels/webhook/inbound`

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
4. Run `pip install -e .`
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
- `IMCODEX_CORE_MODE`: Codex core mode, default `spawned-stdio`
- `IMCODEX_CORE_URL`: optional dedicated Codex core websocket URL
- `IMCODEX_RESTART_EXECUTOR`: optional bridge restart command
- `IMCODEX_DEBUG_API_ENABLED`: enable debug HTTP routes, default `false`
- `IMCODEX_LOG_LEVEL`: Python logging level, default `INFO`
- `IMCODEX_HTTP_HOST`: HTTP bind host, default `0.0.0.0`
- `IMCODEX_HTTP_PORT`: HTTP bind port, default `8000`
- `IMCODEX_OUTBOUND_URL`: optional outbound webhook target
- `IMCODEX_SERVICE_NAME`: client name sent to app-server, default `imcodex`
- `IMCODEX_QQ_ENABLED`: enable QQ bot adapter, default `false`
- `IMCODEX_QQ_APP_ID`: QQ bot AppID
- `IMCODEX_QQ_CLIENT_SECRET`: QQ bot AppSecret
- `IMCODEX_QQ_API_BASE`: QQ API base, default `https://api.sgroup.qq.com`
- `IMCODEX_PYTHON`: Python executable used by helper scripts, default `python`
