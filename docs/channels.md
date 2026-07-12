# Channel Setup and Security

`imcodex` keeps each IM integration as a transport adapter. The adapter owns
platform authentication, native IDs, connection recovery, message limits, and
reply metadata. Codex thread, turn, model, permission, and reasoning state stay
native to Codex.

## Choose a Channel

| Channel | Public callback needed | Conversation scope | Initial capability |
| --- | --- | --- | --- |
| QQ | No | Private and group `@bot` | Text |
| Telegram | No | Private, group, forum topic | Text |
| Feishu / Lark | No | Private, group, topic | Text |
| Weixin iLink | No | Direct only | Text, experimental |
| Generic webhook | Only for remote gateways | Caller-defined | Trusted text injection |

Telegram and Weixin use HTTPS long polling. QQ and Feishu use websocket
connections. All of them make outbound connections from the machine running
`imcodex`; they do not require a public inbound port.

The personal Weixin adapter is not an unofficial desktop-client automation
library. It implements the Tencent iLink flow published in
[`Tencent/openclaw-weixin`](https://github.com/Tencent/openclaw-weixin). The
protocol and account availability can still change, so this adapter is marked
experimental.

## Common Setup

Install the base project for QQ, Telegram, Weixin, and the generic webhook:

```powershell
pip install -e .
```

Feishu/Lark additionally needs the official Channel SDK:

```powershell
pip install -e ".[feishu]"
```

Copy `.env.example` to `.env`, configure one channel, then run:

```powershell
python -m imcodex channels list
python -m imcodex channels doctor
```

Run the doctor again after changing `.env`. It reports whether an enabled
adapter is missing credentials, an optional dependency, or an admission list;
it never prints secret values.

## Admission Is Deny-by-Default

QQ, Telegram, and Feishu use the platform's stable sender ID. Their
`*_ALLOWED_USER_IDS` setting is comma-separated:

```env
IMCODEX_TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

An empty user list denies all inbound users. `*` deliberately allows every
sender and should normally be used only for a short, supervised diagnostic.
Optional `*_ALLOWED_CONVERSATION_IDS` settings can narrow an admitted user to
specific private chats, groups, or topics.

When a sender is denied, `.imcodex-run/current/events.jsonl` records
`message.inbound.access_denied` with the stable `user_id` and normalized
`conversation_id`. A safe discovery loop is:

1. Start with an empty allowlist.
2. Send one message to the bot from the intended owner account.
3. Read the denied event locally.
4. Copy that stable ID into `.env` and restart the bridge.

Do not use display names as identities. Names can change and may not be unique.

## QQ

Create and configure a bot in the
[QQ Bot developer platform](https://bot.q.qq.com/wiki/), then set:

```env
IMCODEX_QQ_ENABLED=1
IMCODEX_QQ_APP_ID=
IMCODEX_QQ_CLIENT_SECRET=
IMCODEX_QQ_API_BASE=https://api.sgroup.qq.com
IMCODEX_QQ_MARKDOWN_ENABLED=1
IMCODEX_QQ_ALLOWED_USER_IDS=
```

The adapter consumes `C2C_MESSAGE_CREATE` and `GROUP_AT_MESSAGE_CREATE`.
Normalized routes are `c2c:<openid>` and `group:<group_openid>`. QQ group
events are already mention-scoped by the platform.

The production API base is shown above. Use the sandbox base only for an app
that is actually configured in the QQ sandbox:

```env
IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com
```

## Telegram

1. Open [BotFather](https://t.me/BotFather) and create a bot.
2. Keep group privacy enabled unless you explicitly need broader group input.
3. Disable arbitrary group joining for a personal bot when appropriate.
4. Put the token in a private file and configure the owner's numeric Telegram
   user ID.

```env
IMCODEX_TELEGRAM_ENABLED=1
IMCODEX_TELEGRAM_BOT_TOKEN_FILE=.telegram-bot-token
IMCODEX_TELEGRAM_ALLOWED_USER_IDS=123456789
IMCODEX_TELEGRAM_REQUIRE_MENTION=1
```

The adapter uses the official Bot API `getUpdates` long-poll flow. Telegram
does not allow `getUpdates` and an active webhook to consume the same bot at
the same time, so remove an existing webhook before starting this adapter.
See the [Telegram Bot API](https://core.telegram.org/bots/api) for the current
platform behavior.

Routes are:

- `chat:<chat_id>` for a private chat or group
- `chat:<chat_id>:topic:<message_thread_id>` for a forum topic

Different forum topics therefore never share one Codex thread accidentally.
Group messages must mention the bot, reply to it, or be a bot command when
`IMCODEX_TELEGRAM_REQUIRE_MENTION=1`.

Telegram responses are sent as plain text in chunks of at most 4,000
characters. Rate limits honor the platform's `retry_after` value, and transient
server errors use bounded retries.

## Feishu and Lark

`feishu` and international `lark` share one adapter and differ only by API
domain.

1. Create an application in the
   [Feishu Open Platform](https://open.feishu.cn/) or
   [Lark Developer](https://open.larksuite.com/).
2. Enable the bot capability.
3. Grant only the message read/receive and send-as-bot permissions needed by
   your intended private/group use.
4. Subscribe to `im.message.receive_v1` using the long-connection mode.
5. Publish/install the app in the intended tenant.
6. Add the owner's stable `open_id` to the allowlist.

```env
IMCODEX_FEISHU_ENABLED=1
IMCODEX_FEISHU_APP_ID=
IMCODEX_FEISHU_APP_SECRET=
IMCODEX_FEISHU_DOMAIN=feishu
IMCODEX_FEISHU_ALLOWED_USER_IDS=ou_xxx
IMCODEX_FEISHU_REQUIRE_MENTION=1
```

For the international service:

```env
IMCODEX_FEISHU_DOMAIN=lark
```

The adapter uses the official
[`lark-channel-sdk`](https://github.com/larksuite/channel-sdk-python), whose
background lifecycle supports readiness, disconnect, and reconnect. SDK
message batching and per-chat agent queues are disabled: imcodex receives each
normalized text message and remains the only bridge layer.

Routes are `chat:<chat_id>` and `chat:<chat_id>:thread:<thread_id>`. Group and
topic messages require a bot mention by default.

## Personal Weixin (Experimental)

The Weixin adapter is text-only and direct-message-only. It requires an iLink
QR login and may not be available to every account or deployment.

Leave the adapter disabled while logging in:

```env
IMCODEX_WEIXIN_ENABLED=0
```

Run:

```powershell
python -m imcodex channels login weixin
```

Open the printed QR link on a device that can display it, scan with mobile
Weixin, and enter the numeric pairing code if requested. A successful login
stores:

- `credentials.json`: bot token, bot account ID, official API base, scanner ID
- `transport.json`: long-poll cursor and per-user context tokens

The default directory is `.imcodex/channels/weixin`. On POSIX systems imcodex
sets the directory to `0700` and both files to `0600`. On Windows, keep
`IMCODEX_DATA_DIR` inside a user-only profile directory so the inherited NTFS
ACL protects the files.

After login:

```env
IMCODEX_WEIXIN_ENABLED=1
```

The scanning user's iLink ID becomes the default owner allowlist. You can
override or extend it explicitly:

```env
IMCODEX_WEIXIN_ALLOWED_USER_IDS=owner@im.wechat
```

Each iLink reply requires the latest `context_token` received from that user.
The user must message the bot at least once before imcodex can send a reply.
If health reports `auth_required` with error `-14`, run the login command
again.

Remove local credentials and protocol state with:

```powershell
python -m imcodex channels logout weixin
```

Media, groups, proactive directory lookup, and local-file delivery are not in
this first version. In particular, imcodex does not let agent output name an
arbitrary local path and exfiltrate that file through Weixin.

## Generic Inbound Webhook

The generic endpoint is for a trusted gateway that normalizes another
transport:

```text
POST /api/channels/webhook/inbound
```

Without a configured token it accepts loopback clients only. Remote gateways
must set:

```env
IMCODEX_INBOUND_WEBHOOK_TOKEN=<long-random-token>
```

and send:

```http
Authorization: Bearer <long-random-token>
Content-Type: application/json
```

Use TLS at the reverse proxy; the built-in HTTP server does not terminate TLS.
The webhook caller is a trusted adapter and chooses `channel_id`, stable sender
ID, conversation ID, and message ID. Do not expose this endpoint as an
unauthenticated public WeChat gateway.

## Health and Recovery

Channel health is written under `.imcodex-run/current/health.json` as
`channels.<channel_id>`. Long-running adapters reconnect in the background and
report `connecting`, `connected`, `reconnecting`, `auth_required`, or `stopped`.

Useful files:

- `.imcodex-run/current/health.json`
- `.imcodex-run/current/events.jsonl`
- `.imcodex-run/current/bridge.log`

Tokens, app secrets, Weixin context tokens, and inbound webhook credentials are
not written to the launch snapshot. Do not paste credential files into issue
reports.

## Windows Notes

- All built-in transports use outbound connections, so a public inbound port
  is not needed.
- Use `pip install -e ".[feishu]"` in PowerShell or Command Prompt to keep the
  extras expression intact.
- Run `python -m imcodex channels doctor` from the same environment used by
  `scripts\start.cmd`.
- Put token files and `IMCODEX_DATA_DIR` under the intended Windows user's
  profile and verify their NTFS ACLs on a shared machine.

## Reference Projects and Deliberate Differences

The implementation was compared with
[`openclaw/openclaw`](https://github.com/openclaw/openclaw),
[`agentscope-ai/QwenPaw`](https://github.com/agentscope-ai/QwenPaw), and
Tencent's [`openclaw-weixin`](https://github.com/Tencent/openclaw-weixin).

imcodex borrows their proven transport patterns—stable native IDs, long-poll
cursors, topic routing, bounded retries, and QR login—but does not copy their
agent/session/plugin runtimes. The existing `BaseChannelAdapter` and middleware
remain the complete channel-to-bridge boundary.

Enterprise WeCom is intentionally not bundled in this release. The current
official Python AI Bot SDK does not yet expose a fully awaitable authenticated
startup and shutdown lifecycle. It can be integrated through the protected
generic webhook today; a native adapter should wait for a lifecycle that can be
cleanly supervised and tested.
