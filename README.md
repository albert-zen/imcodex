# IMCodex

IM to Codex `app-server` bridge.

## Architecture

The codebase now follows a simple three-layer shape plus a thin wiring root:

- `imcodex.channels`
  Adapts concrete IM and transport surfaces such as QQ and the generic webhook API.
- `imcodex.bridge`
  Owns bridge semantics such as slash commands, conversation state routing, and Codex event projection.
- `imcodex.appserver`
  Owns native Codex `app-server` integration, including transport, supervision, and turn/thread lifecycle operations.

Supporting modules:

- `imcodex.composition`
  Builds the runtime graph and wires the three layers together. This is not a fourth business layer; it is the composition root.
- `imcodex.application`
  Exposes the FastAPI app and lifecycle hooks.
- `imcodex.runtime`
  Runs startup and shutdown for the assembled services.
- `imcodex.store`
  Persists bridge state and conversation bindings.

The dependency direction is intentionally one-way:

- `channels` must not depend on `bridge` or `appserver`
- `bridge` may depend on `appserver`, but not on `channels`
- `appserver` must not depend on `bridge` or `channels`

This direction is enforced by architecture tests in [tests/test_architecture.py](/D:/desktop/imcodex/tests/test_architecture.py).

## Run

```powershell
python -m imcodex
```

Helper scripts:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

## QQ Bot

Set these environment variables to enable the built-in QQ adapter:

- `IMCODEX_QQ_ENABLED=1`
- `IMCODEX_QQ_APP_ID=<your AppID>`
- `IMCODEX_QQ_CLIENT_SECRET=<your AppSecret>`
- `IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com`
- `IMCODEX_DEFAULT_PERMISSION_PROFILE=autonomous`

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

## Message Contract

The bridge-visible sync/async contract is documented in
[`docs/message-contract.md`](docs/message-contract.md).

In short:

- normal text gets an immediate `accepted`
- slash commands return one immediate `status`, `command_result`, or `error`
- async progress is `turn_progress`
- async terminal content is `turn_result`
- approvals and user-input requests are `approval_request` and `question_request`

## Deployment

Use [docs/deployment.md](/D:/desktop/imcodex/docs/deployment.md) for a fresh-machine deployment checklist.

The short version is:

1. Install Python 3.13+ and `codex`
2. Copy `.env.example` to `.env`
3. Fill in the required settings
4. Run `pip install -e .`
5. Run `pwsh -File .\scripts\doctor.ps1`
6. Start with `pwsh -File .\scripts\start.ps1`

## Environment

- `IMCODEX_DATA_DIR`: state directory, default `.imcodex`
- `IMCODEX_CODEX_BIN`: codex binary, default `codex`
- `IMCODEX_LOG_LEVEL`: Python logging level, default `INFO`
- `IMCODEX_HTTP_HOST`: HTTP bind host, default `0.0.0.0`
- `IMCODEX_HTTP_PORT`: HTTP bind port, default `8000`
- `IMCODEX_APP_SERVER_HOST`: default `127.0.0.1`
- `IMCODEX_APP_SERVER_PORT`: default `8765`
- `IMCODEX_OUTBOUND_URL`: optional outbound webhook target
- `IMCODEX_SERVICE_NAME`: client name sent to app-server, default `imcodex`
- `IMCODEX_DEFAULT_PERMISSION_PROFILE`: native permission profile for new conversations, `review` or `autonomous`, default `autonomous`
  - `review` defers to native Codex approval/sandbox defaults
  - `autonomous` overrides approval policy to `never` while keeping native sandbox defaults
- `IMCODEX_QQ_ENABLED`: enable QQ bot adapter, default `false`
- `IMCODEX_QQ_APP_ID`: QQ bot AppID
- `IMCODEX_QQ_CLIENT_SECRET`: QQ bot AppSecret
- `IMCODEX_QQ_API_BASE`: QQ API base, default `https://api.sgroup.qq.com`
