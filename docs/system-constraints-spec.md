# System Constraints Spec

Status: Draft

## Goal

This document defines the implementation constraints for `imcodex`.

It is not a user behavior document.
It exists to keep the rewrite aligned with the product model and to prevent the bridge from regrowing a second local agent system.

This document should be read together with:

- [ADR 0001](adr/0001-native-thin-bridge.md)
- [Product Behavior Spec](product-behavior-spec.md)

## Architecture

The system is split into three layers:

1. `Adapter`
2. `Bridge`
3. `App Server`

These are logical layers, not an excuse to duplicate responsibility.

## Layer Responsibilities

### 1. Adapter

The `Adapter` layer exists to integrate specific IM platforms.

It MUST:

- receive platform-native inbound messages
- normalize them into the bridge's inbound format
- send bridge outbound messages to the platform
- maintain platform-specific reply metadata when needed
- keep stable sender identity separate from conversation identity
- apply channel admission before an inbound message reaches bridge commands or
  Codex execution
- bind the admitted stable sender ID to IM reply context and recheck it before
  platform delivery; a stale native binding must not bypass a revoked channel
  access restriction
- hand normalized input off promptly from platform-owned callback threads so
  socket readers never wait for Codex turn work
- allow a bridge-owned long-poll loop to wait for native acceptance and reply
  commit before advancing its platform cursor; this preserves at-least-once
  recovery without adding a second durable message queue, and must not wait for
  asynchronous native turn completion

It MUST NOT:

- own thread truth
- own model truth
- own permission truth
- interpret Codex protocol details
- implement approval logic

### 2. Bridge

The `Bridge` layer exists to connect IM conversation flow to native Codex behavior.

It MUST:

- route one IM conversation to one current native Codex thread
- maintain the minimum local state required for IM continuity
- translate IM commands into native Codex operations
- receive normalized Codex-side events and classify them for display
- apply visibility rules before rendering outbound IM messages

It MUST NOT:

- become a second session engine
- become a second approval engine
- become a second thread directory
- become a second model configuration authority
- become a second permission configuration authority
- become a second reasoning-effort authority
- invent a second required request identity system

### 3. App Server

The `App Server` layer exists to talk to native Codex.

It MUST:

- implement Codex app-server protocol integration
- expose native thread operations
- expose native config operations
- surface native event and request streams upward

It MUST be the only layer that knows Codex protocol details such as:

- thread list/read/start/resume
- turn start/steer/interrupt
- native config read/write
- native approval/request message schema

## Native Authority

Native Codex is the source of truth for:

- thread existence
- thread identity
- thread switching
- thread metadata returned by Codex
- model configuration
- permission configuration
- reasoning-effort configuration
- native request identity
- approval lifecycle

The rewrite MUST use native Codex behavior for all of the above.

The bridge MUST NOT reimplement them as local truth.

## Allowed Bridge State

The bridge MAY keep only the minimum helper state required for IM UX.

Allowed examples:

- current conversation -> current native thread mapping
- current conversation working directory when needed for thread creation UX
- temporary `/threads` browser state
- IM reply metadata
- visibility preferences
- protocol-required channel transport state, such as a polling cursor, a
  platform reply/context token, or a bot credential

This state exists only to support IM interaction.
It does not replace native Codex truth.

Channel transport state MUST be bounded to the protocol need, written
atomically when persisted, protected as sensitive when it contains credentials
or reply tokens, and omitted from launch snapshots and normal diagnostics.

## Forbidden Bridge State

The bridge MUST NOT own or persist a second authority for:

- thread catalog truth
- turn lifecycle truth
- approval truth
- permission truth
- model truth
- reasoning-effort truth
- request identity truth

The bridge MUST NOT require a synthetic bridge-only request id model when native request ids are available.

## Thread Rules

Thread creation, listing, selection, and switching MUST use native Codex operations.

The bridge MAY cache temporary thread-browser page state for IM UX, but:

- the thread list itself remains native truth
- the selected thread id remains native truth

When a thread switch succeeds:

- the bridge MUST tell the user that the switch happened
- the bridge MUST show the effective `CWD`
- the displayed `CWD` must come from native thread state or the successful native switch result, not from an unrelated local guess

## Configuration Rules

Model, permission, reasoning-effort, and personality changes MUST be expressed through native Codex configuration or native thread-level operations.

The default operating configuration MUST align to native Codex settings:

- `approvalPolicy = never`
- `sandbox = danger-full-access`

At connection initialization, if native Codex has no effective modern or legacy permission choice, the bridge MUST seed the native user config with:

- `default_permissions = ":danger-full-access"`
- `approval_policy = "never"`

That seed MUST use native config operations and reload the native user-config stack. It MUST use the native user-layer version for optimistic concurrency, preserve managed defaults and restrictions, and MUST NOT be repeated over an existing native permission choice. It MUST NOT be expressed as a thread start, resume, or turn override. Existing and resumed threads may retain their persisted native thread settings.

The bridge MUST NOT maintain separate long-term per-conversation defaults for:

- model
- permission mode
- reasoning effort
- personality

If the product allows these settings to be changed from IM, the bridge is only a translation layer for native Codex operations.

### Configuration Console Rules

The graphical configuration console is a local presentation and composition
surface. It MUST NOT become another authority for native Codex execution
semantics.

For native Codex settings, the console MUST:

- read effective values and supported choices through the App Server boundary
- project managed new-thread defaults and native feature requirements as effective, read-only state
- write model, reasoning effort, personality, Fast mode, and permission changes
  through native Codex configuration operations
- derive Fast availability from the selected or default native model's advertised
  service tiers, while preserving an off path for a currently configured Fast tier
- treat `service_tier` as the native Fast-mode truth and leave the native
  `features.fast_mode` capability gate unchanged
- treat the successful native response as authoritative and refresh from
  native state instead of retaining a bridge-owned shadow value
- apply interdependent model, reasoning, personality, and service-tier changes as one validated
  native config batch so the page cannot strand an incompatible partial transition
- expose only an explicit allowlist of configuration fields; raw native config
  layers MUST NOT be returned to the browser because unrelated native settings
  may contain secrets

For bridge and channel settings, the console MAY manage the project `.env`, but
it MUST:

- limit writes to an explicit bridge-owned schema
- update the file atomically with optimistic revision checking, preserve
  unmanaged entries and comments, and protect the file as sensitive on
  platforms that expose suitable file permissions
- serialize console writers across processes; unrelated external editors are
  outside that advisory lock and MUST NOT be described as fully coordinated
- report that a bridge restart is required; it MUST NOT claim that `.env`
  changes have been hot-reloaded
- treat values supplied by the process environment as higher-precedence,
  read-only overrides, while launcher values proven to have been imported from
  the managed `.env` remain editable there
- never return stored secret values to the browser or diagnostics; secret
  fields MUST use explicit preserve, replace, and clear operations

Remote channel access policy MUST remain a small adapter-owned gate over stable
platform identifiers. Empty lists and `*` mean that dimension is unrestricted;
`none` explicitly denies all; concrete user and conversation dimensions combine
with `any` by default or `all` when configured. Derived labels such as
`platform`, `restricted_any`, `restricted_all`, and `deny_all` are diagnostics,
not persisted policy state. An intentional `deny_all` policy MUST NOT make
transport readiness degraded.

The console MUST remain loopback-only even when other IMCodex HTTP routes bind
to a non-loopback interface. Every console request MUST pass both a loopback
network-peer check and a loopback `Host` check. Mutating requests MUST also
present a server-issued CSRF token, and browser-originated mutations MUST be
same-origin. The console MUST NOT be treated as a remotely exposable mobile or
web administration protocol.

## Bridge Restart Safety

The built-in restart path MUST fail before stopping the current bridge unless
all of the following are true:

- the launch snapshot is environment-reconstructable rather than derived from
  an untracked explicit `Settings` object
- every externally owned bridge, native Codex, TLS, certificate, and proxy
  environment input named by the snapshot is still present
- reconstructed Settings, local channel prerequisites, the App Server target,
  and the intended HTTP bind address pass a side-effect-free preflight
- a newly selected HTTP port is bindable before the current listener is stopped
- the bridge currently answering at the recorded endpoint reports the exact
  PID and instance ID recorded in the launch snapshot

Replacement health MUST be verified through a bridge-specific HTTP response
that reports the replacement PID and a non-empty instance ID. A listening TCP
port alone is not health evidence. Wildcard bind addresses MAY be probed through
their corresponding loopback address.

On native Windows, built-in restart MUST request shutdown through the running
bridge and allow the normal ASGI lifespan cleanup to finish. It MUST NOT use a
forceful process termination fallback. The control request MUST be loopback-only
and bound to the current instance identity; when graceful control is
unavailable, restart MUST fail closed.

## Approval Rules

The default operating mode is native full-access:

- `approvalPolicy = never`
- `sandbox = danger-full-access`

Under that default operating mode, approval requests are not part of the normal product path and should not appear.

If a native approval request does appear under the default operating mode, it MUST be treated as an exceptional diagnostic path.
Likely causes include:

- incorrect native config during thread start or resume
- thread routing or resume drift in the bridge
- Codex upstream behavior drift
- a bridge bug

If native Codex emits an approval request anyway:

- the bridge MUST surface it using the native request id
- the bridge MUST support batch approval/deny/cancel without an id
- the bridge MUST support unique-prefix targeting by native request id
- a plain non-command user message while approvals are pending MUST cancel pending approvals and continue with the new instruction

The bridge MUST NOT silently drop or indefinitely stall a native approval request.

If the bridge cannot support a native request shape:

- it MUST reject or fail it explicitly
- it MUST NOT leave the upstream side waiting forever

## Projection Rules

The system MUST ingest all native Codex output that reaches the bridge.

The bridge MUST:

1. receive the native output
2. normalize or classify it
3. decide whether it is visible under the current display rules
4. emit the allowed IM-visible message

At minimum, the product recognizes these output categories:

- commentary/progress
- tool calls
- final result
- approval/request interactions

Default display behavior:

- commentary is shown
- tool-call detail is hidden
- final result is shown

Hidden output is still part of the ingest-and-classify path.
It is hidden by policy, not ignored by architecture.

## Transport And Backpressure Rules

Codex App Server transport behavior is a real system constraint, not an implementation detail we can ignore.

Current upstream websocket transport uses bounded outbound queues.
If the bridge becomes a slow consumer and the websocket writer queue fills, App Server may disconnect that websocket client instead of waiting forever.

For a local independently managed App Server, `unix://` is the native control
socket carrier. It uses the same WebSocket protocol as TCP and therefore MUST
use the same initialization, connection-epoch, reconnect, and native
rehydration rules. The socket path and server lifecycle remain native Codex
configuration and MUST NOT be copied into bridge-owned process or PID state.
TCP `ws://`/`wss://` remains a compatibility carrier while upstream documents
that listener as experimental.

On Unix, the project App Server lifecycle CLI MUST delegate to
`codex app-server daemon` and preserve its stdout, stderr, and exit status.
Native Codex owns daemon identity, PID, socket cleanup, version compatibility,
installation checks, and updates. The bridge MUST NOT add a second manifest for
that native daemon. Native Windows is the explicit compatibility exception
while Codex daemon lifecycle remains Unix-only: the existing project core
manager may own one detached local TCP App Server process and its minimal
recovery manifest.

Launchers MUST treat an explicit canonical App Server target as connect-only
and MUST NOT start or select another server. On POSIX, only a completely
unconfigured launcher may ensure the native daemon and select `unix://`; native
Windows MUST instead start or reuse the detached local TCP App Server and select
its canonical `ws://` target. `stdio://` remains explicit compatibility only.
Canonical and legacy target values from the entry process, conda activation,
and `.env` MUST
NOT be mixed across configuration layers; that is their precedence order.

The rewrite MUST assume that:

- websocket notification volume can exceed what a slow bridge consumer can safely absorb
- a hidden message is still transport traffic unless the client opted out of it natively
- stale local turn state after disconnect is a product bug, not just an observability bug

The bridge and App Server adapter MUST therefore follow these rules:

- the socket read path MUST be kept fast and MUST NOT block on slow downstream projection or logging work
- JSON-RPC responses MUST stay on the socket read fast path, while native server requests such as approvals MUST use a bounded dispatch path isolated from ordinary notifications
- native request-resolution notifications MUST preserve wire order with the request they resolve
- IM delivery for a native server request MUST be bounded; failure MUST explicitly reject the native request and remove its local route instead of starving later requests
- dispatch queues MUST be bounded; overflow MUST reset and reconcile the connection explicitly instead of blocking the socket reader, dropping protocol messages silently, or growing memory without limit
- protocol/event logging MUST NOT synchronously slow the transport read path under normal operation
- high-volume native notifications that the default product does not need SHOULD be suppressed using native `optOutNotificationMethods` during `initialize`
- default visibility policy MUST NOT be implemented by merely receiving everything and then doing expensive per-message work for hidden high-frequency deltas
- the websocket client MUST accept legitimate full native thread responses; a bridge-owned frame limit MUST NOT create a permanent reconnect loop for a large `thread/resume` result
- recovery MAY request a bounded recent-turn page only when the experimental native API is enabled, and MUST retry with the stable request shape when that capability is rejected
- reconnect and rehydrate logic MUST reconcile native thread state before trusting any previously cached local `active_turn`
- an established external App Server target MUST reconnect in the background after an unexpected disconnect; a new inbound IM message MUST NOT be the recovery trigger
- an external connection failure MUST NOT silently spawn or select another App Server; `stdio://` is an explicit bridge-child compatibility target only
- background reconnect delay MUST be capped and jittered, while retrying until recovery succeeds or bridge shutdown begins
- connect and initialize work MUST be serialized so that each connection epoch has at most one handshake
- responses, native request routes, and late transport messages from an old connection epoch MUST NOT be accepted by or sent through a newer epoch
- a reconnected transport MUST NOT be reported as restored until native initialize and all ready-time reconciliation handlers complete
- health MUST report `degraded` with reconciliation counts when ready-time rehydration fails or cannot verify one or more native bindings
- cached active-turn authority MUST be cleared before native resume; an active native thread without a verifiable active turn MUST remain degraded
- when a cached active turn completed during a disconnect, recovery SHOULD project its terminal native result, MUST use a stable thread/turn delivery identity to deduplicate queued native notifications, and MUST discard any orphaned message-pump buffer
- a recovered terminal result MUST remain retryable until its IM sink confirms delivery; a transient sink failure MUST NOT consume the only recovery marker
- if ready-time rehydration cannot verify a cached local `active_turn`, recovery MUST discard that untrusted cache rather than continue showing it as `inProgress`
- bridge shutdown MUST cancel reconnect work and finish closing any transport or child process whose teardown has already started

WebSocket target URLs MUST NOT carry userinfo, query, or fragment credentials.
Authentication belongs in the dedicated token or token-file settings so launch
snapshots and diagnostics do not become secret stores.

Managed IM adapters MUST also reconnect without blocking the App Server socket
reader. Their stop path MUST cancel long polls/background connections promptly,
and one adapter's stop failure MUST NOT prevent the remaining adapters and
native client from being closed.

Candidate high-volume notification classes that SHOULD be reviewed for native opt-out in the default product profile include:

- `item/commandExecution/outputDelta`
- `item/reasoning/textDelta`
- any other per-token or per-line delta stream not required by default UX

If a disconnect still happens:

- the bridge MUST promptly mark the connection as lost
- the bridge MUST NOT leave a stale local turn shown as indefinitely `inProgress`
- the next inbound user message MUST NOT be the first moment when stale turn state is corrected

## Failure Rules

User-facing failures MUST be translated into concise product language.

Internal failure handling MUST follow these rules:

- no native request may be left silently pending because the bridge forgot to answer it
- unsupported native request shapes must fail explicitly
- on a shared external App Server, host-registered dynamic tool requests are
  owned by the client that supplied their implementation; IMCodex MUST NOT
  execute a fallback with side effects unless its configured topology declares
  IMCodex to be the thread-tool host. That declaration is enabled by the
  project launchers for the independent App Server they manage, but remains
  disabled for explicit connect-only endpoints. Delegation MUST be bounded and
  canceled when native completion arrives. As the declared host, IMCodex MAY
  translate only operations that map directly to native thread/turn APIs; it
  MUST NOT recreate Desktop-owned project, pin, handoff, or remote-host state.
  Other unresolved dynamic tools MUST fail explicitly rather than leave the
  turn pending. A private bridge-child connection MAY use the same native
  translations, but MUST reject unsupported requests immediately
- protocol details should stay inside the App Server boundary
- the bridge may log diagnostic detail, but must not expose raw protocol noise to end users

## Rewrite Guardrails

Any new implementation should be rejected if it introduces:

- a bridge-owned approval engine as primary truth
- a bridge-owned thread directory as primary truth
- bridge-owned long-term model defaults
- bridge-owned long-term permission defaults
- bridge-owned long-term reasoning-effort defaults
- a second local identity model for requests when native request ids already exist

The intended rewrite direction is:

- thin adapters
- a thin bridge with minimal state
- native Codex as the authority

If a design choice improves convenience but violates that boundary, it should be rejected.
