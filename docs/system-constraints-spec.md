# System Constraints Spec

Status: Draft

## Goal

This document defines the implementation constraints for `imcodex`.

It is not a user behavior document.
It exists to keep the rewrite aligned with the product model and to prevent the bridge from regrowing a second local agent system.

This document should be read together with:

- [ADR 0001](/D:/desktop/imcodex/docs/adr/0001-native-thin-bridge.md)
- [Product Behavior Spec](/D:/desktop/imcodex/docs/product-behavior-spec.md)

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

This state exists only to support IM interaction.
It does not replace native Codex truth.

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

Model, permission, and reasoning-effort changes MUST be expressed through native Codex configuration or native thread-level operations.

The default operating configuration MUST align to native Codex settings:

- `approvalPolicy = never`
- `sandbox = danger-full-access`

The bridge MUST NOT maintain separate long-term per-conversation defaults for:

- model
- permission mode
- reasoning effort

If the product allows these settings to be changed from IM, the bridge is only a translation layer for native Codex operations.

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

The rewrite MUST assume that:

- websocket notification volume can exceed what a slow bridge consumer can safely absorb
- a hidden message is still transport traffic unless the client opted out of it natively
- stale local turn state after disconnect is a product bug, not just an observability bug

The bridge and App Server adapter MUST therefore follow these rules:

- the socket read path MUST be kept fast and MUST NOT block on slow downstream projection or logging work
- protocol/event logging MUST NOT synchronously slow the transport read path under normal operation
- high-volume native notifications that the default product does not need SHOULD be suppressed using native `optOutNotificationMethods` during `initialize`
- default visibility policy MUST NOT be implemented by merely receiving everything and then doing expensive per-message work for hidden high-frequency deltas
- reconnect and rehydrate logic MUST reconcile native thread state before trusting any previously cached local `active_turn`

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
