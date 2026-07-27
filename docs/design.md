# Design

Status: Draft

## Purpose

`imcodex` is a thin IM bridge over native Codex.

The project exists to let IM conversations drive a real Codex core without
re-implementing a second agent runtime, a second approval engine, or a second
thread model inside the bridge.

## Core Product Shape

The intended runtime has five practical surfaces:

- transport adapters under `imcodex.channels`
- bridge logic under `imcodex.bridge`
- native Codex protocol integration under `imcodex.appserver`
- a loopback-only configuration presentation under `imcodex.admin`
- a thin composition/runtime shell that wires them together

`imcodex.admin` belongs to the runtime/composition side of the architecture. It
projects native settings without owning them and manages only the explicit
bridge/channel configuration schema; lower layers do not depend on it.

The bridge should feel conversational in IM, but the native Codex core remains
the authority for:

- thread lifecycle
- turn lifecycle
- approval and request identity
- model / permission / reasoning semantics

The built-in transport set currently includes QQ, Telegram, Feishu/Lark, and
experimental Tencent iLink Weixin. These are peer adapters over the same
bridge contract; no channel owns a separate Codex agent or thread runtime.
All built-in channels and the trusted generic webhook normalize admitted
static JPEG, PNG, and WebP attachments into the same inbound contract and hand them to
native Codex as `localImage` inputs; no transport adapter owns image
understanding.

Platform-native replies use the same boundary. An adapter may normalize the
quoted snapshot delivered with the current event into a shared inbound quote
shape, but it does not own conversation history. Because native Codex exposes
no structured quote user-input item, the bridge renders that snapshot as one
bounded quoted-message text block before the current message. Platform media
URLs are reduced to non-secret type/name/transcript summaries and are never
retained as quote state.

Remote adapters share one optional access-restriction model. Platform delivery
is the default scope; stable user and conversation IDs can narrow that scope,
and `any`/`all` selects how multiple active dimensions combine. This is an IM
transport gate, not a second permission engine and not a prerequisite in the
first connection flow.

## Visual Identity

The product mark is a geometric `IM` monogram: a teal `I`, an ink-coloured
folded `M`, and a teal lower-left cutout. It represents the IM side joining a
native Codex path without turning the bridge into a separate agent product.

Use the mark as a responsive identity system: the vertical primary logo for
large brand surfaces, the horizontal lockup for product chrome, and the mark
alone for favicons or compact controls. Keep it flat, vector, and
monochrome-compatible. Do not replace it with generic chat bubbles, bot faces,
rounded app-icon containers, network-node clip art, gradients, or shadows.

## Native-First Rule

Native Codex source code and protocol behavior are the first source to inspect
before adding bridge behavior.

When implementing a new capability:

1. check whether native Codex already provides it
2. integrate with that native capability directly when it exists
3. only add bridge-owned state or workflow when native Codex does not expose
   the needed behavior and IM still requires it

This keeps `imcodex` thin, inspectable, and easier to recover.

## Bridge-Owned Concerns

The bridge may keep only IM-specific state that native Codex does not own, such
as:

- channel and conversation bindings
- bootstrap context before a native thread exists
- reply context needed by a transport adapter
- the last admitted stable sender ID needed to recheck current channel policy
  before projecting later native output
- IM-only visibility preferences
- minimal request routing needed to complete native flows
- bounded, short-lived output gates used only to preserve IM ordering while a
  native thread binding or idle-history response is being presented
- platform transport cursors and reply tokens required to resume an IM
  protocol, such as Telegram update offsets and Weixin context tokens
- short-lived, privately staged inbound attachments needed to translate a
  platform message into native Codex input

If a change introduces a new local source of truth for something native Codex
already owns, that is a design smell and should be challenged.

Thread-switch output gates hold only native messages that arrive between a
user's switch/history request and delivery of its immediate IM response. They
are never persisted, never used as thread history, and are removed before
normal live projection resumes. Historical content is always read from native
Codex. Gate capacity is bounded; when it is full, the decoupled native event
dispatcher waits for delivery to release capacity instead of dropping final
output or leaving native approval/input requests unresolved. A transient
receive sequence preserves wire order across the independent notification and
server-request dispatch lanes; it is not persisted as thread state. A failed
immediate response keeps its gate bound to that inbound IM message so a durable
cached-response retry still precedes buffered native output; unrelated inbound
messages cannot release it. The gate also retains a transient copy of that
immediate response until delivery succeeds, so bounded response-cache eviction
cannot turn an expired-replay notice into an ordering acknowledgement. Once the
immediate response is delivered, a failed buffered-output send is retried with
bounded backoff using the already projected IM message rather than projecting
the native event twice.

The generic webhook returns ordinary immediate responses in its HTTP response.
Live thread handoff additionally requires its outbound callback because native
messages can arrive after that HTTP exchange has ended. Without a configured
default outbound sink, switch/history commands fail explicitly instead of
creating an output gate that can never drain. Buffered native approval and
question requests retry the same projected message within their native
delivery timeout, rechecking that the native request is still pending before
each attempt; only an exhausted timeout is rejected back to Codex.

Transport credentials and cursors are not native Codex state. A channel may
persist them only when its platform protocol requires them, using private files
that never enter launch snapshots or normal user-visible diagnostics.

Staged attachments follow the same discipline. Each channel owns only its
platform reference, authentication, download, and any required transport
decryption. One shared media boundary owns actual-byte validation, limits, and
private spool cleanup for QQ, Telegram, Feishu/Lark, Weixin, and webhook
uploads. The bridge preserves the user's text-and-image intent, while the App
Server layer alone translates that intent into native protocol types. A
message contains at most four validated static JPEG/PNG/WebP images of at most 10 MiB
and 40 megapixels each; each channel spool is bounded to 512 MiB, expires files
after 24 hours, and sweeps expired files at startup, before media batches, and
hourly. A filesystem lock makes each spool's cleanup, quota check, and batch
write one transaction even when overlapping bridge processes share the same
data directory. Downloads are held in a whole-message memory buffer bounded by
the four-image, 10 MiB-per-image limits. One disposable child process then owns
the filesystem lock, expiry sweep, quota check, private batch write, full decode,
rename, and rollback transaction. Cancellation terminates that child; if its
termination or rollback cannot be confirmed, the materializer retains the
worker handle and fails closed until restart. Download and staging work stays
off platform callback/socket readers,
and permanent media failures become explicit replies rather than hidden drops
or endless queue retries.
Validation uses a maintained decoder and bounded pixel load after the download;
a file-header signature by itself is not a valid image, and animation is
rejected rather than validating only its first frame. Media preparation is
lazy under the existing per-conversation middleware lock so a committed stable
message replay is deduplicated before network or filesystem side effects,
without a second media-specific dedup store.

Because native `localImage` contains a path rather than image bytes, image input
requires the bridge and App Server to share a filesystem namespace. Supporting
a truly remote App Server would require a separate, explicitly designed media
transfer boundary; imcodex must not pretend that a bridge-local path is remotely
readable. The current contract therefore permits local paths only for
bridge-child stdio and the normal Unix-socket daemon, and rejects all TCP
targets even when they use a loopback host. The Unix daemon is an explicit
local-filesystem product
assumption; a containerized deployment must mount the spool at the same
absolute path.

## App Server Runtime Direction

The preferred runtime shape for day-to-day IM use is:

- a long-lived native Codex App Server
- a separately restartable IM bridge
- native recovery first, local cleanup only as a fallback

This direction is preferred over bridge-managed private cores because it keeps
native thread and approval state alive across bridge restarts and makes
observability clearer.

The normal product shape has one external App Server target. Unix and TCP are
transport facts, not different ownership modes, and the bridge cannot observe a
meaningful difference between the old `dedicated-ws` and `shared-ws` labels.
The target URL is therefore the canonical configuration.

Outbound IM delivery is the one restart boundary that needs a durable bridge
checkpoint. While a bound native turn is running, the Turn watch stores only
its thread/turn identity and means “this IM route is still owed a terminal
result”; it does not say whether the turn is active. Rehydration asks native
Codex for that status. Once projected, the checkpoint also carries the exact
outbound message and stable delivery ID until the channel sink accepts it.
This outbox is delivery state, not a copy of native turn history, and ownership
moves with the conversation-to-thread binding until projection. Once staged,
the message keeps its exact IM route and survives native binding cleanup until
the sink accepts it. Explicit standalone channel delivery enters this same
service-owned outbox and managed artifact spool; its HTTP/tool boundary never
calls a channel sink directly or creates a parallel retry runtime. A bounded
standalone acknowledgement retains the final per-artifact delivery outcome so
an idempotent replay, including after restart, returns the original receipt
rather than inferring success from the message-level acknowledgement.

The Agent-facing repository launcher forwards native `CODEX_THREAD_ID` but
does not copy an IM route. The running bridge remembers the last IM recipient
that explicitly selected each thread. That minimal routing fact survives the
conversation switching to a newer thread, while selecting the same thread from
another conversation moves its recipient. Route authority therefore remains
in the bridge, parallel tasks cannot steal one another's destinations, and
explicit channel/conversation targeting remains a lower-level operator
interface.

Native lifecycle, visible answer projection, and IM delivery have different
granularities and must not share one implicit terminal flag. A
`final_answer` agent-message item closes one visible answer segment; it does
not complete the native Turn. Only native Turn lifecycle events such as
`turn/completed` are authoritative for completion. Queued steering can make
the same Turn emit another item after a visible answer and later emit another
`final_answer`, so the message pump may reopen presentation state without
changing native Turn truth.

Native projection keeps the original Codex discriminator. Agent output uses
`agentMessage` with its native `phase`; plan and diff notifications use their
native methods; goal, status, warning, and native request notifications keep
their native methods; command and file items use their native item types;
completion fallbacks use `turn/completed`. IM presentation classes such as
“progress”, “result”, “approval”, or “status” may be derived at a UI boundary,
but they are not persisted or used as bridge lifecycle truth.

The durable outbox must use the identity of the unit actually being delivered.
A Turn-level watch is sufficient to recover a completion that happened while
the bridge was offline, but each projected answer item needs an item/segment
identity so multiple answers from one Turn can coexist and retry independently.
A pending delivery for one answer segment must never suppress, overwrite, or
consume later commentary or answer segments from the same native Turn. Durable
answer segments for one IM destination drain in projection order; a failure in
one destination does not block outbox progress for a different destination.
That order is an explicit durable outbox sequence, not an assumption about
dictionary iteration order or wall-clock timestamp uniqueness.

Snapshot recovery enumerates every nonblank native `final_answer` item in
native order. Explicit artifacts produced after the last answer segment are
owned by a separate `turn/completed` fallback so they do not silently disappear
or change an earlier item's delivery identity. If native data omits item IDs
and two answer items are otherwise indistinguishable, recovery stays degraded
and retains its watch instead of conflating the two deliveries.

After a sink acknowledges an answer segment, the bridge retains only its
delivery identity alongside the Turn watch until native Turn completion. This
small receipt prevents a restart between answer acknowledgement and
`turn/completed` from replaying the answer or inventing an empty completion
fallback. It is IM delivery truth, not native Turn state, and is removed when
the watch is consumed.

Structured agent-sent artifacts are part of this delivery state, not native
thread state. The bridge stages only explicit native output references into a
private content-addressed spool; it does not scan a workspace or infer files
from file-change events. Explicit final-answer Markdown image nodes outside
fenced and inline code retain automatic preview delivery, including native
temporary outputs outside the thread workspace. Ordinary Markdown links are
only native-surface navigation; generic files require structured native
output or explicit standalone delivery. Channel adapters translate artifacts
into their platform-native upload/send operations. Per-artifact channel APIs
use one shared batch checkpoint contract: confirmed artifacts record receipts,
retryable failures preserve the failed artifact plus the unattempted suffix,
and permanent failures become visible notices without blocking later
artifacts. A generic multipart webhook remains batch-atomic because its
transport accepts one message envelope; retry therefore preserves the whole
batch. Text remains the existing outbound message projection and is not
wrapped in a second Markdown message type.

Every new input to an already bound thread crosses a native resume/reconcile
barrier first. This makes the exact native `threadId` authoritative after work
from Desktop, CLI, or another IMCodex connection, while refusing to guess when
an active native turn cannot be verified.

`stdio://` remains an explicit bridge-child compatibility target for tests and
older installations. It MUST NOT be selected as a fallback after an external
target fails. Native Windows keeps an external two-process shape through the
project-managed detached TCP App Server until native daemon lifecycle is
available there. Legacy mode names are accepted only at the configuration
boundary and are normalized before runtime behavior begins.

See also:

- [Product Behavior Spec](product-behavior-spec.md)
- [System Constraints Spec](system-constraints-spec.md)
- [ADR 0001](adr/0001-native-thin-bridge.md)
