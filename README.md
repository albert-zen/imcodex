# IMCodex

IM to Codex `app-server` bridge.

## Run

```powershell
python -m imcodex
```

## QQ Bot

Set these environment variables to enable the built-in QQ adapter:

- `IMCODEX_QQ_ENABLED=1`
- `IMCODEX_QQ_APP_ID=<your AppID>`
- `IMCODEX_QQ_CLIENT_SECRET=<your AppSecret>`
- `IMCODEX_QQ_API_BASE=https://api.sgroup.qq.com`

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
  "text": "/projects"
}
```

## Environment

- `IMCODEX_DATA_DIR`: state directory, default `.imcodex`
- `IMCODEX_CODEX_BIN`: codex binary, default `codex`
- `IMCODEX_APP_SERVER_HOST`: default `127.0.0.1`
- `IMCODEX_APP_SERVER_PORT`: default `8765`
- `IMCODEX_OUTBOUND_URL`: optional outbound webhook target
- `IMCODEX_SERVICE_NAME`: client name sent to app-server, default `imcodex`
- `IMCODEX_QQ_ENABLED`: enable QQ bot adapter, default `false`
- `IMCODEX_QQ_APP_ID`: QQ bot AppID
- `IMCODEX_QQ_CLIENT_SECRET`: QQ bot AppSecret
- `IMCODEX_QQ_API_BASE`: QQ API base, default `https://api.sgroup.qq.com`
