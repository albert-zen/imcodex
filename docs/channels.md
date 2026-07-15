# Channel Setup and Security

`imcodex` keeps each IM integration as a transport adapter. The adapter owns
platform authentication, native IDs, connection recovery, message limits, and
reply metadata. Codex thread, turn, model, permission, and reasoning state stay
native to Codex.

## Choose a Channel

| Channel | Public callback needed | Conversation scope | Initial capability |
| --- | --- | --- | --- |
| QQ | No | Private and group `@bot` | Text, JPEG/PNG/WebP images |
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
adapter is missing credentials or an optional dependency; it never prints
secret values. Access restrictions are optional and are not a readiness check.

## Access Follows the Platform by Default

QQ, Telegram, Feishu, and Weixin share one optional access policy over stable
platform IDs. With no restrictions configured, imcodex accepts messages that
the platform delivers: private messages, and mention-scoped group messages
where supported. A group restriction admits the group without requiring every
member to be listed.

Concrete user and conversation restrictions are comma-separated:

```env
IMCODEX_TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
IMCODEX_TELEGRAM_ALLOWED_CONVERSATION_IDS=chat:-100123456
```

Each empty list, or a list containing `*`, leaves that dimension unrestricted.
Enter `none` by itself in either list to keep the channel connected while
accepting nobody. Across both lists, do not combine `none` with any other value.

When both concrete dimensions exist, choose how they combine:

```env
IMCODEX_TELEGRAM_ACCESS_MATCH=any
```

- `any` is the long-term default: a listed user anywhere, or anyone in a listed
  conversation, is accepted.
- `all` accepts only listed users inside listed conversations.

With only one concrete dimension, `any` and `all` behave identically. To stop
the connection entirely, set the channel's `*_ENABLED=0` instead of using an
access restriction.

When a sender is denied, `.imcodex-run/current/events.jsonl` samples
`message.inbound.access_denied` with the stable `user_id` and normalized
`conversation_id`. Repeated denials are rate-limited so a public bot cannot
grow logs without bound. IDs remain available for diagnostics and for an
operator who later chooses to add restrictions; they are not part of initial
connection.

Channel health distinguishes transport connectivity from the derived policy
display. Under `.imcodex-run/current/health.json`, adapters report
`inbound_access_ready`, `access_policy_mode`, `access_match`, and non-secret
restriction counts. Modes are `platform`, `restricted_any`, `restricted_all`,
and `deny_all`. Empty restrictions and intentional `none` are both ready states;
only connectivity and channel prerequisites affect readiness. A sampled denial
also records `last_inbound_access_denied_at` and a non-secret reason.

Do not use display names as identities. Names can change and may not be unique.
Every accepted identity is an operator, not a low-privilege chat user: it can
use commands such as `/cwd`, `/config`, and `/native` and can reach the native
threads visible to this Codex account. Narrow the platform scope in advanced
settings when that exposure is not appropriate.

## QQ

Create and configure a bot in the
[QQ Bot developer platform](https://bot.q.qq.com/wiki/), then set:

```env
IMCODEX_QQ_ENABLED=1
IMCODEX_QQ_APP_ID=
IMCODEX_QQ_CLIENT_SECRET=
IMCODEX_QQ_API_BASE=https://api.sgroup.qq.com
IMCODEX_QQ_MARKDOWN_ENABLED=1
```

The adapter consumes `C2C_MESSAGE_CREATE` and `GROUP_AT_MESSAGE_CREATE`.
Normalized routes are `c2c:<openid>` and `group:<group_openid>`. QQ group
events are already mention-scoped by the platform.

Inbound images work without an additional switch, image-model setting, user
list, or group-member list. The same access policy applies to text and images:
with no optional restrictions configured, any private sender or member of a QQ
group that the platform delivers may use the bot, and group images still
require the platform's `@bot` event. Access is checked before an attachment is
downloaded.

The P0 image contract is:

- JPEG, PNG, and WebP are accepted only after a maintained image decoder
  verifies the downloaded structure; matching magic bytes alone is not enough
- a message may contain only images, or text plus images
- one message may contain at most four images
- each downloaded image may be at most 10 MiB
- each image may contain at most 40 megapixels before decode
- limits reject the affected message explicitly; images are never silently
  truncated or dropped

QQ attachment URLs are transport data, not Codex input. The adapter downloads
accepted images outside the gateway socket reader, stages them in a private,
bounded spool, and forwards their local paths through native Codex
`localImage` inputs. Spool files expire after 24 hours and the spool has a 512
MiB total bound. Structural verification includes an actual bounded pixel
decode, so a truncated JPEG/WebP payload cannot pass on its header alone.
Expired files are swept at startup, before a new media batch,
and hourly while QQ is running, so physical deletion can lag expiry by at most
one cleanup interval while the channel is idle. If the adapter cannot validate
or download an image, or cannot safely stay within those bounds, it replies
with a concise error instead of silently ignoring the message or blocking later
QQ messages. A whole-batch deadline also prevents a slowly dripping media
response from occupying the inbound worker indefinitely.

Media preparation is lazy and runs inside the middleware's per-conversation
serialization boundary. A committed stable QQ `message_id` replay is resolved
from the existing dedup/reply record before any attachment network or spool
work. Likewise, onboarding and a known incompatible App Server topology are
preflighted before download rather than staging a file that cannot be submitted.

`localImage` is a filesystem reference. This P0 path therefore requires the
bridge and Codex App Server to run in the same filesystem namespace. imcodex
only submits these paths over bridge-child stdio or the normal local Unix-socket
target. Every TCP App Server target, including `ws://127.0.0.1`, is rejected for
image input because loopback can still be an SSH tunnel, WSL, or a container
boundary; text remains usable. A containerized Unix-socket deployment is valid
only when it mounts the media spool at the same absolute path as the bridge.
The Unix transport is the product's local-daemon assumption, not a protocol
proof of mount-namespace identity.

On POSIX, the spool directory and files are restricted to `0700` and `0600`.
On Windows, imcodex installs a protected current-user-only DACL and rejects
symlink, junction, and other reparse-point spool roots before cleanup.

The production API base is shown above. Use the sandbox base only for an app
that is actually configured in the QQ sandbox:

```env
IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com
```

No openid or group ID is required for first use. Add optional restrictions only
after the bot is connected if the QQ platform scope is broader than intended.

## Telegram

1. Open [BotFather](https://t.me/BotFather) and create a bot.
2. Keep group privacy enabled unless you explicitly need broader group input.
3. Disable arbitrary group joining for a personal bot when appropriate.
4. Put the token in a private file.

```env
IMCODEX_TELEGRAM_ENABLED=1
IMCODEX_TELEGRAM_BOT_TOKEN_FILE=.telegram-bot-token
IMCODEX_TELEGRAM_REQUIRE_MENTION=1
```

On POSIX, create the token file with mode `0600`; imcodex rejects a symlink or
a file readable by group/other users. The recommended `.telegram-bot-token`
path is ignored by this repository. On Windows, keep it under the intended
user profile and verify the inherited NTFS ACL.

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
characters. Polling rate limits honor the platform's `retry_after` value and
connection failures use bounded exponential backoff. Outbound `sendMessage`
does not automatically retry an ambiguous timeout or 5xx response, because the
first request may already have been delivered; a 429 response is safe to retry
after the platform-supplied delay. Poll offsets are bound to the current bot ID
and expire before Telegram's long-idle update-ID randomization window. An
existing corrupt offset file fails startup explicitly; inspect it before
removing it to reset polling, because a blind reset can replay queued commands.

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

```env
IMCODEX_FEISHU_ENABLED=1
IMCODEX_FEISHU_APP_ID=
IMCODEX_FEISHU_APP_SECRET=
IMCODEX_FEISHU_DOMAIN=feishu
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
normalized text message and remains the only bridge layer. imcodex enables the
SDK's strict websocket security, bounds websocket fragments and handler
concurrency, then feeds admitted messages through its own bounded FIFO worker.
This keeps callbacks fast while preventing two messages from racing the same
bridge state.

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

The scanning user's iLink ID becomes the default user restriction when the
user list is empty. Setting `IMCODEX_WEIXIN_ALLOWED_USER_IDS=*` deliberately
uses the broader platform scope; a concrete list replaces the owner default:

```env
IMCODEX_WEIXIN_ALLOWED_USER_IDS=owner@im.wechat
```

Each iLink reply requires the latest `context_token` received from that user.
The user must message the bot at least once before imcodex can send a reply.
If health reports `auth_required` with error `-14`, run the login command
again, then restart the bridge so it loads the new credential. Login and logout
commands print the same restart reminder. If a state file is corrupt or has
unsafe POSIX permissions, startup fails explicitly instead of resetting the
cursor and risking command replay; use logout plus a fresh login to reset it.

Remove local credentials and protocol state with:

```powershell
python -m imcodex channels logout weixin
```

Stop or restart an already running bridge after logout, because that process
may still hold the old credential in memory until shutdown.

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
ID, conversation ID, and message ID. Authentication runs before JSON parsing,
and the endpoint rejects bodies over 64 KiB and text over 32 KiB. Generic
callers cannot claim the reserved `qq`, `telegram`, `feishu`, or `weixin`
channel IDs; choose a dedicated namespace such as `wecom-gateway`. The bridge
persists the most recent 1,024 committed `message_id` values per conversation
and drops retries in that bounded window. A gateway must retain its own
longer-term idempotency history when delayed replay is possible. To keep bridge
state bounded, only the most recent 32 immediate response bodies are retained.
If an older ID is still inside the dedupe window but its response body has been
evicted, imcodex returns an explicit `cached_response_expired` error message
instead of silently acknowledging it with an empty response.

The HTTP response contains any immediate command/status messages:

```json
{"messages":[{"channel_id":"wecom-gateway","conversation_id":"conv-1","message_type":"accepted","text":"Accepted","request_id":null,"metadata":{"delivery_id":"imcodex:..."}}]}
```

Normal Codex prompts usually finish asynchronously. Set both
`IMCODEX_OUTBOUND_URL=https://gateway.example/outbound` and a separate
`IMCODEX_OUTBOUND_WEBHOOK_TOKEN`; imcodex POSTs immediate messages and later
native results to that URL using this payload. The deterministic
`metadata.delivery_id` is attached to both replayable immediate replies and
native projections so callback retries remain safe to deduplicate.

```json
{
  "channel_id": "wecom-gateway",
  "conversation_id": "conv-1",
  "message_type": "turn_result",
  "text": "...",
  "request_id": null,
  "metadata": {"delivery_id": "imcodex:native:..."}
}
```

The gateway must return a 2xx response. Non-2xx responses are surfaced as
delivery failures instead of being silently treated as sent. It must verify
`Authorization: Bearer <IMCODEX_OUTBOUND_WEBHOOK_TOKEN>`. Treat this callback
as the canonical delivery path; the inbound HTTP response also contains a
convenience copy of immediate messages. Recent committed retries replay the
cached immediate response and retry failed callback delivery without executing
the command again. Immediate callbacks are therefore at-least-once while the
gateway keeps retrying inside the documented cache window. Native projections
use bounded in-process callback retries, not a second durable bridge outbox; if
all attempts fail, recover the result from the native Codex thread. Every
callback retry carries the same deterministic `metadata.delivery_id`, so the
gateway must deduplicate by that value. Plain HTTP and a missing token are
accepted only for a loopback outbound URL. Do not expose the inbound endpoint
as an unauthenticated public WeChat gateway.

## Health and Recovery

Channel health is written under `.imcodex-run/current/health.json` as
`channels.<channel_id>`. Long-running adapters reconnect in the background and
report `connecting`, `connected`, `reconnecting`, `auth_required`, or `stopped`.

Useful files:

- `.imcodex-run/current/health.json`
- `.imcodex-run/current/events.jsonl`
- `.imcodex-run/current/bridge.log`

Tokens, app secrets, Weixin context tokens, and inbound webhook credentials are
not written to the launch snapshot. Dependency wire loggers stay at WARNING
even when `IMCODEX_LOG_LEVEL=DEBUG`, preventing Bot API URLs and websocket auth
frames from entering bridge logs. Do not paste credential files into issue
reports.

QQ image diagnostics record only bounded operational metadata such as attachment
count, validated media type, byte count, processing stage, and error class. They
do not record image content, signed attachment URLs, original filenames, or
staged local paths. A single media failure does not make a connected QQ channel
unhealthy.

## Windows Notes

- All built-in transports use outbound connections, so a public inbound port
  is not needed.
- Use `pip install -e ".[feishu]"` in PowerShell or Command Prompt to keep the
  extras expression intact.
- Run `python -m imcodex channels doctor` from the same environment used by
  `scripts\start.cmd`.
- The normal detached Windows App Server uses loopback TCP, so QQ image input is
  intentionally unavailable in this P0; text remains available. A future
  explicit media-transfer/topology capability can remove this conservative
  boundary without guessing from `localhost`.
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
