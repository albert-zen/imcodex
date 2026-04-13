# IMCodex Native App-Server Architecture Proposal

This document proposes a more complete redesign for `imcodex` after reviewing:

- the current `imcodex` bridge implementation
- OpenAI's Codex App Server docs
- the upstream `openai/codex` source and protocol notes

The target is not "make the current bridge slightly nicer".
The target is to make `imcodex` behave like a thin IM surface over native Codex
thread, turn, item, and approval semantics.

## 0. IM-Native Product Constraints

This redesign is specifically for an IM channel surface, not for a desktop or
IDE UI.

That means the bridge must preserve native Codex semantics while respecting
three hard constraints of IM:

1. The channel is append-only from the user's point of view.
2. Interaction is primarily text, with only limited file attachment support.
3. Every control action must be executable through explicit commands sent as
   normal IM messages.

Practical consequences:

- approvals must be completed through slash-command-style messages
- session/permission/visibility settings must be changed through commands
- state inspection must be readable through command output, not hidden UI
- progress must be emitted as append-only messages or coalesced summaries, not
  as rich in-place UI widgets
- native Codex capabilities should be projected into text-first interaction
  patterns instead of custom GUI affordances

## 1. External Constraints We Should Design Around

The redesign should treat the following upstream facts as architectural inputs,
not optional implementation details.

### 1.1 App Server is a bidirectional event protocol, not request/response RPC

App Server exposes a bidirectional JSON-RPC-like protocol where one client
request can produce many server notifications, and the server can also initiate
requests such as approvals or user-input prompts.

Implication for `imcodex`:

- the bridge cannot treat `turn/start` as the whole truth
- turn lifecycle must be driven by notifications as well as request results
- pending server requests must be tracked as first-class state

### 1.2 The native model is thread -> turn -> item

OpenAI's docs and upstream code both center the product around:

- `thread` as the durable session container
- `turn` as one unit of user-initiated work
- `item` as the atomic unit of output such as agent messages, commands, file
  changes, plans, reasoning, MCP calls, review items, and approvals

Implication for `imcodex`:

- IM-visible state should be derived from thread/turn/item, not from a bridge-
  invented workflow model
- projection should be item-aware, not only final-text-aware

### 1.3 Native thread operations already exist

App Server already supports native lifecycle methods and notifications such as:

- `thread/start`
- `thread/resume`
- `thread/fork`
- `thread/list`
- `thread/read`
- `thread/name/set`
- `thread/archive`
- `thread/unsubscribe`
- `thread/name/updated`
- `thread/closed`

Implication for `imcodex`:

- the bridge should stop acting like its local store is the primary source of
  truth for thread metadata
- local persistence should be small and mostly cache/binding oriented

### 1.4 Native permission profiles are richer than the current bridge abstraction

App Server and upstream Codex already model:

- approval policy
- sandbox policy
- approvals reviewer
- request-specific approval decisions
- `serverRequest/resolved`
- network-specific approval context
- additional permission requests in newer flows

Implication for `imcodex`:

- `IMCODEX_AUTO_APPROVE` is too small as the long-term policy surface
- approval handling should be profile-based and request-type aware

### 1.5 Event surface is intentionally non-exhaustive

Upstream protocol notes explicitly say request and event enums are
non-exhaustive, and App Server docs continue adding new notifications.

Implication for `imcodex`:

- projection code must degrade gracefully on unknown event types
- storage and message pump design should be extensible by category, not by
  hard-coded event whitelist

### 1.6 One session runs one active task at a time

Upstream protocol notes describe one active task per session/thread and
recommend separate Codex instances or threads of work for parallel tasks.

Implication for `imcodex`:

- one IM conversation should bind to one primary native thread at a time
- background parallelism should be modeled as additional native threads, not as
  multiple active turns hidden inside one bridge conversation state blob

## 2. Current Gaps In IMCodex

After reviewing the current code, the main gaps are structural rather than
incremental.

### 2.1 The store still owns too much bridge-defined semantics

`ConversationStore` currently persists:

- project registry
- project-to-cwd identity
- thread registry
- turn activity
- pending request mapping
- thread labels
- known thread lists per conversation

This is useful for bootstrap, but it also means the bridge is still acting like
its own session platform. That conflicts with native thread portability.

### 2.2 The bridge still teaches `project` as a primary concept

Current commands and status output still expose:

- `/projects`
- `/project use`
- `project id`

This is the opposite of the `cwd`-first direction already captured in repo docs.

### 2.3 The projector only covers a narrow slice of the native event stream

Today the bridge mostly handles:

- agent message deltas
- final agent messages
- command approvals
- file-change approvals
- user-input prompts
- command/file summaries on completion

It does not yet treat the following as first-class IM-visible or state-bearing
events:

- `serverRequest/resolved`
- `thread/name/updated`
- `turn/plan/updated`
- `turn/diff/updated`
- reasoning summary deltas
- MCP tool progress
- permissions approvals beyond command/file change
- newer dynamic tool or collaboration item types

### 2.4 The backend is still a lifecycle owner, not just a native session shim

`CodexBackend` currently decides recovery behavior, thread binding, steer retry,
and local fallback behavior in one place. Some of that belongs in a native
session manager, but some belongs in a higher-level orchestration layer with a
clear state machine.

### 2.5 There is no real message pump yet

The current projector emits messages opportunistically. That is enough for a
prototype, but not enough for:

- per-turn deduplication
- stale-turn suppression
- final-result precedence
- approval lifecycle cleanup
- category-based visibility policies
- channel-specific coalescing/throttling

## 3. Design Goals

The new architecture should optimize for:

1. Native-thread portability across restart and Codex surfaces.
2. Minimal local persistence.
3. Clear IM-visible lifecycle for turn progress, approvals, and final output.
4. Extensible support for new App Server item and request types.
5. Stable behavior on Windows and long-lived local bridge processes.

## 4. Proposed Target Architecture

Keep the existing three-layer repo shape:

1. `channels`
2. `bridge`
3. `appserver`

But tighten responsibilities significantly.

### 4.1 `appserver`: transport plus typed native primitives

Responsibilities:

- transport connection management
- initialize and capability negotiation
- typed request wrappers
- typed notification and server-request decoding
- app-server process supervision and diagnostics

Should own:

- reconnection logic
- overload retry policy for transport-level retryable errors
- request id generation
- optional notification opt-out configuration

Should not own:

- IM wording
- conversation binding policy
- ticket numbering
- user-facing visibility preferences

Recommended additions:

- support explicit initialize capabilities, especially
  `optOutNotificationMethods`
- type and surface `serverRequest/resolved`
- add wrappers for `thread/list`, `thread/read`, `thread/name/set`,
  `thread/archive`, and optionally `thread/fork`
- preserve native `thread.path`, `name`, `status`, and returned thread turns

### 4.2 `bridge.session_registry`: minimal conversation binding

Introduce a small registry whose job is only to bind an IM conversation to a
native Codex thread and local user preferences.

Suggested persisted record:

- `channel_id`
- `conversation_id`
- `selected_cwd`
- `thread_id`
- `active_turn_id`
- `active_turn_status`
- `last_inbound_message_id`
- `visibility_profile`
- `pending_request_ids`
- `last_seen_thread_name`
- `last_seen_thread_path`
- `last_seen_thread_status`

Explicitly do not persist as primary state:

- a bridge-owned `project` abstraction
- duplicated thread item history
- synthesized final answers
- long-lived command/file buffers outside the message pump

Internal note:

- if a normalized cwd key is still useful for deduplication, keep it internal
  and never show it to users as a first-class concept

### 4.3 `bridge.thread_directory`: native thread discovery cache

Add a bridge service that can query and cache native thread metadata through:

- `thread/list`
- `thread/read`

Responsibilities:

- populate `/threads` and attach/select flows from native thread state
- refresh cached thread labels, path, cwd, status, and recent preview
- support cold-start restoration after bridge restart

This component makes local state smaller because thread browsing no longer
depends on a bridge-owned historical registry.

### 4.4 `bridge.turn_state_machine`: one state machine per bound thread

Replace ad-hoc turn bookkeeping with an explicit state machine keyed by:

- `thread_id`
- `turn_id`

Suggested states:

- `idle`
- `starting`
- `in_progress`
- `awaiting_approval`
- `awaiting_user_input`
- `interrupting`
- `completed`
- `failed`
- `interrupted`

Responsibilities:

- resolve whether an inbound user message should `turn/steer` or `turn/start`
- gate stale notifications after interrupt or thread switch
- track latest active request ids for a turn
- decide when a terminal IM message may be emitted

### 4.5 `bridge.request_registry`: native server-request tracking

Pending requests should become a first-class registry keyed by native request id
plus user-facing ticket id.

Suggested record:

- `native_request_id`
- `ticket_id`
- `thread_id`
- `turn_id`
- `item_id`
- `request_method`
- `request_kind`
- `created_at`
- `resolved_at`
- `status`

Important behavior:

- resolve requests on `serverRequest/resolved`, not only on local reply success
- support more than command/file approvals and question prompts
- keep request type open-ended for future App Server additions

### 4.6 `bridge.message_pump`: the core missing component

Introduce a dedicated outbound message pump that consumes normalized native
events and emits IM-facing messages.

Responsibilities:

- per-conversation outbound ordering
- per-turn grouping
- throttling/coalescing
- deduplication
- stale-turn suppression
- final-result precedence
- channel-specific reply-context attachment

The message pump should be the only place that decides whether an event becomes:

- a visible chat message
- a coalesced update
- a state-only update
- a suppressed event

### 4.7 `bridge.visibility_classifier`: category-based event visibility

Map native events into a small, stable set of IM visibility categories:

- `progress`
- `plan`
- `search`
- `files`
- `commands`
- `approvals`
- `questions`
- `reasoning`
- `system`

Suggested user-facing profiles:

- `minimal`
- `standard`
- `verbose`

This matches the upstream App Server direction better than a growing set of
one-off message types.

## 5. Canonical Lifecycle Rules

The bridge needs a single source of truth for lifecycle handling.

### 5.1 Starting a new conversation

Recommended flow:

1. User selects `cwd` or bridge auto-selects the single available cwd.
2. Bridge calls `thread/start` with native permission profile and
   `persist_extended_history` enabled.
3. Bridge stores only the native thread binding and minimal conversation
   metadata.
4. User text triggers `turn/start`.
5. IM gets immediate `accepted`.
6. All meaningful later updates come from notifications.

### 5.2 Resuming after restart

Recommended flow:

1. Load conversation binding from local registry.
2. Attempt `thread/read` to validate the bound thread.
3. If metadata looks good, call `thread/resume`.
4. If resume succeeds, refresh local cached thread metadata from the returned
   thread snapshot.
5. If read/resume fails, mark binding stale and surface a recoverable IM status
   message rather than silently starting a fresh thread.

Key rule:

- do not silently replace a stale native thread with a new one unless the user
  explicitly chooses recovery

### 5.3 Handling a follow-up user message while work is active

Recommended flow:

1. If the active thread has an in-progress turn, try `turn/steer`.
2. If `turn/steer` fails with a retryable "not active yet" style error, retry
   briefly once.
3. If the server reports a real terminal or invalid-turn condition, transition
   the local state machine and start a fresh turn intentionally.
4. Suppress stale output from the superseded turn in the message pump.

### 5.4 Approval and question lifecycle

Recommended flow:

1. `item/started` may introduce the relevant command/file/tool item.
2. Server request arrives with native request id and thread/turn scope.
3. Bridge allocates an IM-visible ticket id and sends a user-facing prompt.
4. User replies with approval or answers.
5. Bridge forwards the native request response.
6. Bridge keeps the request pending until `serverRequest/resolved`.
7. `item/completed` and later `turn/completed` close the loop.

Key rule:

- native request resolution, not just local send success, closes pending IM
  state

### 5.5 Final-result precedence

Recommended flow:

- use `item/completed(agentMessage phase=final_answer)` for early useful output
- do not consider the turn fully settled until `turn/completed`
- if `turn/completed` says `failed` or `interrupted`, it overrides optimistic
  earlier final text

This preserves fast UX without losing correctness.

## 6. User-Facing Surface Changes

### 6.1 Make `cwd` the only primary workspace concept

Keep:

- `/cwd <path>`
- `/status`
- `/threads`
- `/thread attach <thread-id>`
- `/new`
- `/stop`

Deprecate or hide by default:

- `/projects`
- `/project use`
- visible `project id`

If compatibility is needed, keep old commands as aliases only.

### 6.2 Improve thread identity in IM

Use native thread naming features plus local fallback rules:

1. native thread name if present
2. native preview if present
3. clipped first user message
4. raw thread id as last resort

Also expose `thread_id` separately in status or advanced views, not as the
primary label.

### 6.3 Add visibility controls

Suggested future commands:

- `/view minimal`
- `/view standard`
- `/view verbose`

This keeps the message contract stable while adapting to different IM channel
noise tolerances.

Required item-layer controls:

- final reply is always shown
- commentary is shown by default and can be hidden
- tool calls are hidden by default and can be shown

### 6.4 Make commands the only control plane

Because the IM surface has no reliable native widget model, all non-freeform
control actions should be command-driven.

This includes:

- approvals
- denials
- request answers
- permission profile selection
- visibility profile selection
- cwd selection
- thread attach/new/recovery
- state and diagnostics queries

Design rule:

- if an action changes bridge state or responds to a native App Server request,
  it should be representable as a command message

Suggested command families:

- workspace and thread
  - `/cwd <path>`
  - `/threads`
  - `/thread attach <thread-id>`
  - `/new`
  - `/recover`
- approvals and questions
  - `/approve <ticket...>`
  - `/approve-session <ticket...>`
  - `/deny <ticket...>`
  - `/cancel <ticket...>`
  - `/answer <ticket> key=value`
- settings
  - `/permissions autonomous`
  - `/permissions review`
  - `/view <profile>`
  - `/show commentary`
  - `/hide commentary`
  - `/show toolcalls`
  - `/hide toolcalls`
  - `/model <name>`
- state and diagnostics
  - `/status`
  - `/requests`
  - `/doctor`
  - `/thread read`

The bridge may still emit normal natural-language progress and final answers,
but control flow should remain command-addressable.

### 6.5 Design for append-only channel history

The IM client cannot rely on rich message mutation or structured panes.

Therefore the bridge should prefer:

- concise accepted messages
- bounded progress updates
- explicit terminal messages
- ticket-based approval prompts
- command-oriented state snapshots

The bridge should avoid:

- assuming editable UI cards
- relying on hidden local controls
- dumping every token delta as a new message

### 6.6 File output should be optional and sparse

Since the IM surface can only carry text plus limited file attachments, file
artifacts should be reserved for cases where plain text is not sufficient.

Recommended policy:

- default to text summaries in-channel
- attach files only for diffs, logs, or structured outputs that are too large or
  too lossy to inline
- when files are attached, announce them in a normal channel message so the
  thread remains understandable from text alone

## 7. Permission Model Proposal

Move from one boolean-ish auto-approve switch to named native permission
profiles.

Permission profile is a native Codex concept, not a bridge-invented one.
The IM layer should expose a simplified command surface over native policy.

Suggested profiles:

- `autonomous`
  Codex acts autonomously without asking the user during normal work.
- `review`
  Codex returns to the default manual-review posture.

Each profile should map to native App Server inputs:

- `approval_policy`
- `sandbox_policy`
- `approvals_reviewer`

Optional environment variable support can remain, but it should set a native
permission profile rather than inventing a separate bridge-only mode.

## 8. Reliability And Windows Guardrails

### 8.1 Startup diagnostics should be first-class

Expose or log:

- codex executable path
- app-server transport
- app-server host/port
- bridge host/port
- process id
- data directory
- current permission profile

### 8.2 Connection and overload handling

App Server docs mention bounded queues and overload behavior in WebSocket mode.
The client layer should therefore distinguish:

- transport disconnected
- request timed out
- server overloaded / retryable
- thread stale / not resumable
- turn invalid / not steerable

These should not all collapse into one generic bridge error path.

### 8.3 Unknown event handling

Unknown notifications should be:

- recorded in logs
- ignored safely by default
- optionally surfaced in verbose mode under `system`

That protects forward compatibility with upstream protocol growth.

## 9. Testing Strategy

The redesign should add scenario tests around the actual product contract.

Minimum set:

1. Cold start -> `thread/start` -> `turn/start` -> progress -> final result.
2. Restart -> `thread/read` + `thread/resume` -> continue same conversation.
3. Approval request -> user reply -> `serverRequest/resolved` -> turn finishes.
4. User-input request -> answer -> request resolved -> turn finishes.
5. `turn/steer` during active work, including retryable not-yet-steerable case.
6. Stale thread detection where resume fails but bridge does not silently fork.
7. `turn/plan/updated` and `turn/diff/updated` visibility behavior.
8. Unknown notification handling does not break message delivery.

## 10. Migration Plan

### Phase A: Build the native-first foundation

- add typed App Server support for missing thread and request methods
- add initialize capabilities support
- add a normalized event envelope for notifications and server requests

### Phase B: Replace store-heavy semantics

- introduce minimal conversation binding registry
- add native thread directory cache
- stop teaching `project` in the user-facing layer

### Phase C: Introduce the message pump

- route all outbound IM messages through a single message pump
- implement per-turn state machine and request registry
- add `serverRequest/resolved` handling

### Phase D: Expand native visibility support

- add plan, diff, reasoning, MCP progress, and thread-name updates
- add visibility profiles

### Phase E: Remove legacy compatibility weight

- demote old `project` commands to compatibility aliases
- delete duplicated local registries that are no longer needed
- make native thread discovery the default browsing flow

## 11. Recommended Immediate Build Slice

If this redesign is implemented incrementally, the highest-leverage first slice
is:

1. Add `thread/list`, `thread/read`, and `serverRequest/resolved` support.
2. Introduce the minimal conversation binding model.
3. Build the message pump with terminal precedence and stale-turn suppression.
4. Rework `/status`, `/threads`, and `/thread attach` to be native-thread aware.
5. Add one restart-resume integration test and one approval-resolution test.

This slice would already move `imcodex` from "bridge with native transport" to
"native Codex surface for IM".

## 12. References

- OpenAI blog: https://openai.com/index/unlocking-the-codex-harness/
- OpenAI App Server docs: https://developers.openai.com/codex/app-server
- Upstream protocol notes:
  `D:\\desktop\\codex-upstream\\codex-rs\\docs\\protocol_v1.md`
- Upstream TUI app-server client/session mapping:
  `D:\\desktop\\codex-upstream\\codex-rs\\tui\\src\\app_server_session.rs`
- Upstream TUI notification adapter:
  `D:\\desktop\\codex-upstream\\codex-rs\\tui\\src\\app\\app_server_adapter.rs`
