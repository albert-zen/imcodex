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

The default composition is now an `im-agent-sdk` Gateway with the SDK Codex
Application and native Channel adapters. IMCodex supplies only product
controller/presentation policies, generic webhook namespace adaptation,
configuration, launch topology, health rendering, and a product command
policy. Gateway bindings, projection/recovery, request
correlation, delivery planning, and delivery submission identity are SDK
owned. The command policy contains only IMCodex product commands and
authoritative native Thread/settings calls; it is not a compatibility runtime.

The final SDK composition uses the accepted typed seams independently:
Codex live activity presentation receives only bounded normalized activity
facts; App Server artifact materialization receives stable completed-item and
terminal facts and returns only typed attachments; destination visibility uses
the routed O1 context; and clean-process spool lease release observes the final
logical O2 outcome. IMCodex does not restore the retired combined presentation
hook or inspect raw native notifications to bridge gaps between these seams.

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
- minimal product routing needed to resolve an IM command to an SDK-owned
  request or authoritative native Thread operation
- platform transport cursors and reply tokens required to resume an IM
  protocol, such as Telegram update offsets and Weixin context tokens
- short-lived, privately staged inbound attachments needed to translate a
  platform message into native Codex input

If a change introduces a new local source of truth for something native Codex
already owns, that is a design smell and should be challenged.

The SDK owns App Server subscriptions, normalized projection, delivery
idempotency, checkpoints, and request correlation. IMCodex does not mirror its
event stream, maintain an output gate, or keep a native-event journal.
Historical content is always read from native Codex. The `/native events`
diagnostic therefore fails explicitly: exposing raw Application events would
recreate a consumer subscription and an untyped extension seam.

The generic webhook returns ordinary immediate responses in its HTTP response.
Live output additionally requires its outbound callback because messages can
arrive after that HTTP exchange has ended. The SDK owns retry and correlation;
IMCodex supplies only product presentation and channel composition.

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

Outbound IM idempotency, projection checkpoints, retry, and recovery belong to
the SDK Gateway. IMCodex keeps no second Turn watch, message pump, or delivery
outbox. The one-time legacy migration may drain already-persisted terminal
deliveries from older releases, but new work is never written to that format.
Explicit standalone delivery enters SDK proactive delivery; its HTTP/tool
boundary never calls a Channel sink directly or creates a parallel retry
runtime.

The Agent-facing repository launcher forwards native `CODEX_THREAD_ID` but
does not copy an IM route. The running bridge remembers the last IM recipient
that explicitly selected each thread. That minimal routing fact survives the
conversation switching to a newer thread, while selecting the same thread from
another conversation moves its recipient. Route authority therefore remains
in the bridge, parallel tasks cannot steal one another's destinations, and
explicit channel/conversation targeting remains a lower-level operator
interface.

Native lifecycle, visible answer projection, and IM delivery have different
granularities and must not share one implicit terminal flag. A `final_answer`
agent-message item closes one visible answer segment; it does not complete the
native Turn. The SDK Application adapter observes native lifecycle and
authoritative history; IMCodex receives only bounded typed A1 facts.

Native projection keeps the original Codex discriminator. Agent output uses
`agentMessage` with its native `phase`; plan and diff notifications use their
native methods; goal, status, warning, and native request notifications keep
their native methods; command and file items use their native item types;
completion fallbacks use `turn/completed`. IM presentation classes such as
“progress”, “result”, “approval”, or “status” may be derived at a UI boundary,
but they are not persisted or used as bridge lifecycle truth.

The SDK uses the stable identity of the unit actually delivered. Recoverable
A1 output must be reconstructable from authoritative history; live-only
activity never advances a checkpoint. O1 suppression completes outbound
idempotency before checkpoint CAS, and O2 observes one logical delivery
attempt without becoming a durable observer or changing retry decisions.

Artifact bytes, local-path trust, file validation, quotas, and cleanup remain
consumer-owned. IMCodex stages only typed A1 candidates or explicit proactive
uploads into a private bounded spool; it does not scan a workspace. Proactive
paths are leased in a crash-safe bounded ledger before SDK submission, keyed by
stable delivery ID. Startup sweep preserves restored leases, retryable O2
outcomes retain them, and terminal outcomes release them. Channel adapters
translate artifacts into platform-native upload/send operations and surface
partial or permanent failures truthfully.

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
