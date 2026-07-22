# Channel Setup and Security

`imcodex` keeps each IM integration as a transport adapter. The adapter owns
platform authentication, native IDs, connection recovery, message limits, and
reply metadata. Codex thread, turn, model, permission, and reasoning state stay
native to Codex.

## Choose a Channel

| Channel | Public callback needed | Conversation scope | Initial capability |
| --- | --- | --- | --- |
| QQ | No | Private and group `@bot` | Text, images, and supported files |
| Telegram | No | Private, group, forum topic | Text, images, and supported files |
| Feishu / Lark | No | Private, group, topic | Text, images, and supported files |
| Weixin iLink | No | Direct only | Text, images, and supported files, experimental |
| Generic webhook | Only for remote gateways | Caller-defined | Trusted text, image, and file injection |

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

## Shared Image Input

Every built-in channel and the generic webhook feed images through the same
native-first pipeline. There is no image feature switch, image access mode, or
separate bridge-owned vision model. Each transport only retrieves its own
platform bytes; the shared layer then validates and stages them before the App
Server receives native Codex `localImage` inputs.

The common contract is:

- static JPEG, PNG, and WebP are accepted only after a maintained image decoder
  verifies and fully loads the downloaded structure
- a message may contain only images, or text plus images
- image-only messages receive a neutral `[Image]` text item at the native
  boundary so the corresponding user turn remains visible in Codex App
- one platform message may contain at most four images
- each image may be at most 10 MiB and 40 decoded megapixels
- a message that exceeds a limit is rejected as a whole; images are never
  silently truncated or dropped
- access restrictions, topology preflight, and stable-message dedup run before
  built-in platform downloads or any managed-spool work

The private spool normally lives under `IMCODEX_DATA_DIR` at
`channels/<channel>/inbound-media`; a custom Weixin state directory carries the
same `inbound-media` child. Each channel spool is bounded to 512 MiB;
at most 16,384 directory entries are accepted before new media fails closed;
files expire after 24 hours and are swept at startup, before a new media batch,
and hourly while the channel is running. Cleanup, quota accounting, and each
batch write share a filesystem lock, so overlapping processes cannot each
consume the same remaining quota. Downloaded bytes remain in a whole-message
memory buffer bounded by four images at 10 MiB each. One disposable child owns
the filesystem lock, directory sweep, quota check, private batch write, bounded
pixel decode, rename, and rollback transaction. The 30-second whole-batch
deadline can terminate that child without abandoning a filesystem worker or
releasing its lock early. If termination or rollback cannot be confirmed, that
materializer retains the worker handle and rejects later image work until the
service restarts. A truncated JPEG or WebP cannot pass on a matching header,
and animated images are rejected rather than validating only their first frame.
Permanent download, decryption, format, and limit failures become concise
user-visible errors and do not block later messages. A 30-second whole-batch
deadline prevents a slowly dripping source from occupying a conversation
indefinitely.

Media preparation is lazy inside the middleware's existing per-conversation
serialization boundary. A committed stable message replay is resolved from the
normal dedup/reply record before media I/O; there is no media-specific durable
queue or second dedup authority.

`localImage` is a filesystem reference. Image input therefore requires the
bridge and Codex App Server to see the same absolute spool path. imcodex submits
images over bridge-child stdio, the normal local Unix-socket target, or the
project-managed Windows TCP App Server after the launcher verifies its process
identity and health. An explicitly configured `ws://127.0.0.1:<port>` is also
accepted when the same project core manifest, exact listener owner, live Codex
command, and readiness probe verify it on every connection. Other TCP targets
are rejected because reachability alone cannot prove a shared filesystem; text
remains usable.
A containerized Unix-socket deployment must mount the spool at the same
absolute path.

On POSIX, spool directories and files use `0700` and `0600`. On Windows,
imcodex installs a protected current-user-only DACL and rejects symlink,
junction, and other reparse-point spool roots before cleanup.

## Shared Agent Artifact Output

QQ, Telegram, Feishu/Lark, Weixin, and the generic outbound webhook consume the
same structured artifacts from a terminal Codex result. Images are delivered as
native platform previews when the platform supports them; generic files are
delivered as platform files. Artifacts are sent before the terminal text, and
there is no channel-specific feature switch.

Only validated files copied into the private
`IMCODEX_DATA_DIR/outbound-media` spool are eligible. When an adapter returns a
failure, the durable retry retains only the artifacts not yet accepted. QQ and
Weixin derive stable platform delivery identities, and Feishu supplies a stable
SDK UUID. Telegram has no equivalent client idempotency key: it does not retry
an ambiguous upload inside the adapter, but the durable outbox may replay it
after an ambiguous live failure or a process exit before the bridge checkpoints
progress.

The generic outbound webhook uses JSON for text-only messages. A message with
artifacts uses `multipart/form-data`: the `payload` field contains the normal
outbound JSON contract plus the artifact manifest, and each file is carried in
a repeated `artifacts` field. Receivers should deduplicate by `delivery_id`.

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

Inbound images use the shared contract without an additional switch. The same
access policy applies to text and images. With no optional restrictions
configured, any private sender or member of a QQ
group that the platform delivers may use the bot, and group images still
require the platform's `@bot` event. Access is checked before an attachment is
downloaded.

QQ attachment URLs remain transport data, never Codex input. The adapter
accepts only HTTPS URLs on the documented Tencent media host families, refuses
redirects, and never records signed URLs in normal diagnostics.

QQ group passive replies have a short platform window. Immediate replies reuse
the inbound `msg_id`; asynchronous output uses it only while that context is
still fresh. A long-running or restart-recovered result automatically falls
back to proactive delivery after the passive window expires, so an old reply
identifier cannot silently strand the final answer.
The original receive time is carried with cached reply metadata, so replaying a
cached response cannot bypass that expiry check. Before a durable terminal
message enters the outbox, QQ pins whether it is proactive or which `msg_id` it
replies to, then derives a stable `msg_seq` from the bridge delivery ID. If the
first HTTP acknowledgement is lost, later inbound traffic or passive-window
expiry cannot change that retry identity, and QQ can deduplicate the repeated
`msg_id + msg_seq` instead of presenting a second final answer.

QQ terminal results may include structured Codex image artifacts. IMCodex
uploads each staged image through the conversation's `/files` endpoint with
`srv_send_msg=false`, sends the returned `file_info` as native `msg_type=7`,
and only then sends the final Markdown text. Each media send derives a stable
identity from the terminal delivery ID, so an acknowledgement-loss retry uses
the same `msg_seq`. C2C generic files use QQ file type 4; QQ group generic files
fail explicitly because that surface does not support them. Only files in the
private `IMCODEX_DATA_DIR/outbound-media` spool can reach this path.

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

Private chats accept a Bot API `photo` or an image `document`, with or without
a caption. In groups, a caption may mention the bot; a captionless image must
reply to a bot message when mention enforcement is enabled. `photo` contains
several sizes of the same image, so imcodex selects the largest pixel area and
downloads it through `getFile`. Declared document MIME, filename, dimensions,
and size are candidate hints only; the shared byte and decoder checks remain
authoritative. A Telegram media album arrives as several stable messages, so
each item enters the normal middleware independently; imcodex does not add a
local album buffer.

Bot tokens appear in Telegram's API and file-download URL paths. The adapter
keeps downloads on the configured Bot API origin, rejects absolute, traversal,
query-bearing, and redirected `file_path` values, and never places a token,
`file_id`, or download URL in normal events.

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
normalized message and remains the only bridge layer. imcodex enables the
SDK's strict websocket security, bounds websocket fragments and handler
concurrency, then feeds admitted messages through its own bounded FIFO worker.
This keeps callbacks fast while preventing two messages from racing the same
bridge state.

Routes are `chat:<chat_id>` and `chat:<chat_id>:thread:<thread_id>`. Group and
topic messages require a bot mention by default.

The official Channel SDK exposes normalized image resource descriptors.
imcodex accepts direct-message images and rich-text posts with inline images,
then calls the message-resource API with the stable `message_id` and
`file_key`; it does not create a second SDK media cache. Duplicate descriptors
in a rich post are collapsed before the shared four-image limit. Tenant-token
refresh uses the official HTTP endpoint through a cancellable 10-second async
path and caches the result only until its refresh window.

A pure group image has no place to carry a Feishu `@bot` mention. With the
default mention requirement, use a rich-text post containing both the mention
and image. Pure group images require both
`IMCODEX_FEISHU_REQUIRE_MENTION=0` and the platform permission to receive all
group messages; imcodex does not silently weaken either platform or local
admission policy.

## Personal Weixin (Experimental)

The Weixin adapter is direct-message-only and accepts text and images. It
requires an iLink QR login and may not be available to every account or
deployment.

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

Inbound `type=2` image items contain a signed Tencent CDN locator and may also
contain an AES key. The adapter follows Tencent's published iLink flow: it
downloads only from an HTTPS `weixin.qq.com` host without redirects or bot
Bearer credentials, decrypts AES-128-ECB/PKCS7 media when a key is present,
then passes plaintext bytes to the shared validator. Signed query parameters,
AES keys, context tokens, and local paths never enter normal events. Image-only
messages still update the sender's required `context_token` before dispatch.

Remove local credentials and protocol state with:

```powershell
python -m imcodex channels logout weixin
```

Stop or restart an already running bridge after logout, because that process
may still hold the old credential in memory until shutdown.

Groups, proactive directory lookup, and outbound local-file delivery are not
in this version. In particular, imcodex does not let agent output name an
arbitrary local path and exfiltrate that file through Weixin.

## Generic Inbound Webhook

The generic endpoint is for a trusted gateway that normalizes another
transport:

```text
POST /api/channels/webhook/inbound
```

Without a configured token it accepts loopback clients only. Loopback JSON
requests need no extra credential, while loopback multipart requests must send
the explicit `X-IMCodex-Webhook: 1` header. Because browser forms cannot set
that non-simple header, a page from another origin cannot silently upload to
the local bridge. Tokenless requests carrying a browser `Origin` are rejected.
Remote gateways and browser-origin callers must set:

```env
IMCODEX_INBOUND_WEBHOOK_TOKEN=<long-random-token>
```

and send either JSON text or multipart text-and-image input:

```http
Authorization: Bearer <long-random-token>
Content-Type: application/json
```

For images, repeat the `images` file field and send routing values as multipart
form fields:

```bash
curl -X POST https://bridge.example/api/channels/webhook/inbound \
  -H "Authorization: Bearer $IMCODEX_INBOUND_WEBHOOK_TOKEN" \
  -F channel_id=wecom-gateway \
  -F conversation_id=conv-1 \
  -F user_id=u1 \
  -F message_id=m1 \
  -F text='describe these' \
  -F images=@first.png \
  -F images=@second.jpg
```

For a local call without a configured Bearer token, use the same multipart
fields against loopback and replace the authorization line with:

```bash
-H "X-IMCodex-Webhook: 1"
```

The caller uploads bytes; it cannot submit `local_path`, `attachments`, or
`input_error`. Authentication and the streaming body bound run before form
parsing. JSON bodies remain limited to 64 KiB. Multipart bodies are limited to
41 MiB total, form text to 32 KiB, and image files then pass through the same
per-image count, byte, format, pixel, and spool checks as built-in channels.
Stable-message dedup occurs before an authenticated upload is copied into the
managed spool. At most two multipart requests parse and retain their
temporary form files concurrently in one bridge process. Once an upload is
copied into the managed spool—or dedup/preflight decides no copy is needed—the
form is closed and its slot is released before downstream Codex handling or
delivery. Capacity wait, multipart parsing, conversation-queue wait, staging,
and form close share a 30-second retention deadline and return HTTP 408 when
exceeded, so slow uploads or a busy conversation cannot retain both parser
slots indefinitely. If an operating-system-backed temporary-file close itself
does not finish within a short post-timeout grace period, parser capacity is
still released and the app retains the cleanup task until it completes or the
process exits; the stuck close cannot delay HTTP 408 or new ingress.
This bounds aggregate parser spooling without changing the JSON path.

Use TLS at the reverse proxy; the built-in HTTP server does not terminate TLS.
The webhook caller is a trusted adapter and chooses `channel_id`, stable sender
ID, conversation ID, and message ID. Generic callers cannot claim the reserved
`qq`, `telegram`, `feishu`, or `weixin`
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

Image diagnostics record only bounded operational metadata such as attachment
count, validated media type, byte count, processing stage, and error class. They
do not record image content, signed attachment URLs, original filenames, or
staged local paths. A single media failure does not make a connected channel
unhealthy.

## Windows Notes

- All built-in transports use outbound connections, so a public inbound port
  is not needed.
- Use `pip install -e ".[feishu]"` in PowerShell or Command Prompt to keep the
  extras expression intact.
- Run `python -m imcodex channels doctor` from the same environment used by
  `scripts\start.cmd`.
- The normal launcher-managed Windows App Server supports image input. An
  explicit canonical `ws://127.0.0.1:<port>` also supports it when IMCodex can
  verify the matching project-managed core process; `localhost`, remote, TLS,
  and unrecorded loopback targets remain text-only.
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

## Generic Inbound Files

QQ, Telegram, Feishu, Weixin, and the generic webhook normalize ordinary
attachments into the same managed input path. Supported files are PDF, UTF-8
text, Markdown, common source code, and common text configuration formats.
Files are limited to 25 MiB each and four per message. Platform filenames and
MIME labels are hints: imcodex bounds the name and validates actual bytes before
placing a randomized private file in the channel media spool.

Images continue to use native `localImage`. Generic files use native Codex
`mention` input with the staged filename and path. Both require a verified
shared filesystem between imcodex and App Server. Unsupported, oversized,
binary, damaged, or unavailable files produce a visible channel error. Generic
webhook callers use repeated multipart fields named `files`; image fields
remain `images`.

## Explicit Agent Delivery

An agent or operator can send through the already-running bridge:

```bash
python -m imcodex channels send \
  --channel telegram \
  --conversation chat:123456 \
  --text "Analysis complete." \
  --artifact reports/result.md
```

The command emits one JSON receipt and exits `0` for confirmed delivery, `3`
for partial delivery, `1` for failed/unconfirmed delivery, and `2` for invalid
local input. Artifact paths must be regular non-symlink files under the current
working directory. The CLI uploads bytes to a loopback-only endpoint protected
by the current bridge instance identity and a private per-process credential,
so it neither exposes arbitrary local paths to HTTP nor creates competing
channel connections. Request-scoped upload files are removed after the channel
adapter returns.

Use `--delivery-id` when retrying an attempted operation. If omitted, the CLI
generates one. Per-artifact receipts include platform message identity when the
adapter exposes it and adapter delivery identity where supported.
