# IMCodex Native-First Refactor Master Plan

This document turns the native-first architecture direction into a large-scale,
execution-oriented refactor plan.

It should be read together with:

- [im-native-channel-spec.md](./im-native-channel-spec.md)
- [native-app-server-architecture.md](./native-app-server-architecture.md)
- [im-native-interaction-model.md](./im-native-interaction-model.md)
- [native-redesign-plan.md](./native-redesign-plan.md)
- [next-step-development-plan.md](./next-step-development-plan.md)
- [message-contract.md](./message-contract.md)
- [issue-notes.md](./issue-notes.md)

The intent is to answer five practical questions:

1. What are we rebuilding?
2. What stays stable during the refactor?
3. In what order should work happen?
4. What code should move where?
5. How do we know we are done?

## 0. Product Constraint: IM Channel Is The UI

This refactor assumes the primary user experience is an IM channel.

That gives us these hard product constraints:

- interaction is append-only and message-based
- the channel mostly supports text, with only limited file attachments
- approvals, settings, and state queries must all be executable through
  commands sent as messages

So the target is not "mini desktop Codex inside chat".
The target is "native Codex semantics projected into a command-driven,
text-first IM surface".

## 1. Refactor Thesis

The current codebase already has the correct outer layering:

- `channels`
- `bridge`
- `appserver`

But the inside of the bridge still behaves like a custom session platform.

The refactor thesis is:

- `appserver` should expose native Codex capabilities cleanly
- `bridge` should bind IM conversations to native Codex threads
- `channels` should only translate platform I/O
- local persistence should be minimal and recoverable
- IM-visible state should be derived from native thread/turn/item events

This is a foundational refactor, not a cosmetic cleanup.

## 2. Non-Negotiable Outcomes

By the end of the refactor, the system should satisfy all of these:

1. A conversation can resume a native Codex thread after bridge restart.
2. `/threads` can be driven from native thread data, not only bridge-local
   history.
3. Pending approvals and questions are resolved by native request lifecycle,
   including `serverRequest/resolved`.
4. The bridge can render more than final prose: plan, diff, command/file
   activity, approvals, and selected tool progress.
5. `cwd` is the only primary workspace concept in the user-facing layer.
6. The bridge remains deployable and debuggable on Windows throughout the
   migration.
7. All approvals, settings, and status operations are addressable through
   command messages.
8. The IM-visible experience remains readable in append-only channel history.
9. Permission switching maps onto native Codex permission profiles.
10. Batch approval of multiple pending tickets is supported.
11. Item visibility supports separate control for final reply, commentary, and
    tool calls.

## 3. What Must Stay Stable During Refactor

To avoid a rewrite stall, these should remain stable unless a phase explicitly
changes them:

- FastAPI entrypoints
- QQ and webhook channel adapters
- basic slash command availability
- current immediate versus async message contract
- app-server supervision model

This lets us refactor inward without breaking all existing integration points at
once.

## 4. Target Package Layout

The current package layout can stay, but the bridge layer should be split into
smaller responsibilities.

### 4.1 `src/imcodex/appserver`

Target modules:

- `client.py`
  low-level transport, request ids, initialize, typed request helpers
- `protocol_map.py`
  normalization layer from raw JSON payloads into internal event envelopes
- `session_client.py`
  high-level thread and turn lifecycle operations
- `supervisor.py`
  process lifecycle, diagnostics, startup checks
- `diagnostics.py`
  runtime environment, executable path, host/port inspection

### 4.2 `src/imcodex/bridge`

Target modules:

- `service.py`
  replaces orchestration currently in `core.py`
- `session_registry.py`
  minimal conversation-to-thread bindings
- `thread_directory.py`
  native thread discovery and cache
- `turn_state.py`
  per-thread turn state machine
- `request_registry.py`
  native request tracking and IM ticket mapping
- `message_pump.py`
  outbound ordering, coalescing, suppression, final precedence
- `visibility.py`
  category classification and visibility profiles
- `commands.py`
  user command parsing and command-to-operation mapping
- `rendering.py`
  user-facing message text templates

### 4.3 `src/imcodex/channels`

This layer should stay thin. It may grow small helper modules for platform
reply context and sequencing, but it should not absorb bridge logic.

## 5. Refactor Workstreams

Run the refactor as five coordinated workstreams.

## Workstream A: Native App-Server Surface

### Goal

Make `appserver` a complete typed wrapper over the native App Server methods and
notifications we need.

### Deliverables

- typed wrappers for:
  - `thread/list`
  - `thread/read`
  - `thread/name/set`
  - `thread/archive`
  - `thread/fork` if needed for future flows
- typed handling for:
  - `serverRequest/resolved`
  - `thread/name/updated`
  - `turn/plan/updated`
  - `turn/diff/updated`
  - reasoning summary notifications
  - MCP tool progress notifications
  - broader approval request families
- initialize capabilities support
- overload-aware retry policy where transport semantics require it

### Code likely touched

- [client.py](/D:/desktop/imcodex/src/imcodex/appserver/client.py:1)
- [backend.py](/D:/desktop/imcodex/src/imcodex/appserver/backend.py:1)
- [supervisor.py](/D:/desktop/imcodex/src/imcodex/appserver/supervisor.py:1)
- tests under `tests/test_appserver_*`

### Exit criteria

- the bridge never has to inspect raw notification method strings outside a
  single protocol mapping boundary
- native thread metadata can be queried on demand

## Workstream B: State Model Reduction

### Goal

Shrink the local persistence model so it stores bindings, not a second session
platform.

### Deliverables

- new minimal session registry schema
- migration loader from old store schema
- internal-only cwd normalization support
- demotion of `project` from primary concept to compatibility alias

### Current pain points this addresses

- [store.py](/D:/desktop/imcodex/src/imcodex/store.py:39) currently persists
  project registry, thread registry, turn activity, pending requests, and label
  derivation in one object
- the current state shape is helpful locally but too heavy to be the long-term
  truth

### Proposed persisted record shape

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

### Migration strategy

1. Add read compatibility for current store payloads.
2. Write both old and new fields during one transition phase if needed.
3. Switch readers to the new model.
4. Remove project-first logic after command layer cutover.

### Exit criteria

- `/status` no longer needs to show `project id`
- thread browsing can recover from native thread APIs
- local data loss does not imply native thread loss

## Workstream C: Message Pump And Turn State

### Goal

Introduce a proper outbound message pump and explicit turn lifecycle state
machine.

### Deliverables

- normalized internal event envelope
- per-turn buffers with stale-turn suppression
- final-result precedence
- request lifecycle tracking tied to native resolution
- category-based coalescing and visibility decisions

### Why this matters

Right now [projection.py](/D:/desktop/imcodex/src/imcodex/bridge/projection.py:8)
is acting as parser, accumulator, renderer, and lifecycle coordinator at the
same time. That is the highest-risk hotspot in the current design.

### Suggested internal pipeline

1. Raw App Server notification/request.
2. Normalize to internal event envelope.
3. Update session registry / turn state / request registry.
4. Hand event to visibility classifier.
5. Hand visible event to message pump.
6. Message pump emits zero or more outbound chat messages.

### Turn state machine states

- `idle`
- `starting`
- `in_progress`
- `awaiting_approval`
- `awaiting_user_input`
- `interrupting`
- `completed`
- `failed`
- `interrupted`

### Exit criteria

- the bridge can safely emit early final text and still be corrected by terminal
  turn state
- interrupted or superseded turns do not keep leaking stale progress into chat

## Workstream D: User Surface Simplification

### Goal

Converge the user-facing experience on `cwd` plus native thread identity.

### Deliverables

- revised `/status`
- revised `/threads`
- revised `/thread attach`
- compatibility handling for `/projects` and `/project use`
- clearer thread naming rules using native names where available
- visibility profile commands if desired in the first pass
- command-driven permission and settings surface
- append-only-friendly message wording and ticket prompts
- commentary/tool-call visibility toggles
- batch approval command semantics

### Recommended command policy

Primary commands:

- `/cwd <path>`
- `/status`
- `/threads`
- `/thread attach <thread-id>`
- `/new`
- `/stop`
- `/approve`
- `/approve-session`
- `/deny`
- `/cancel`
- `/answer`
- `/permissions autonomous`
- `/permissions review`
- `/view <profile>`
- `/show commentary`
- `/hide commentary`
- `/show toolcalls`
- `/hide toolcalls`
- `/requests`
- `/doctor`

Compatibility aliases:

- `/projects`
- `/project use`

### Exit criteria

- a new user can operate the bridge without learning `project id`
- thread labels are readable without hiding canonical thread ids from power
  users

## Workstream E: Reliability, Diagnostics, And Test Coverage

### Goal

Keep the system operational while internals are moving.

### Deliverables

- richer startup diagnostics
- environment and port ownership reporting
- restart and stale-thread recovery tests
- approval resolution tests
- turn steering tests
- visibility/message-pump regression tests

### Recommended test buckets

- unit tests
  state transitions, request registry, visibility classifier
- component tests
  appserver client mapping, thread directory behavior
- integration tests
  webhook and QQ visible lifecycle
- restart tests
  persisted binding plus `thread/read`/`thread/resume`

## 6. Program Phases

These phases are intentionally sequenced so each one leaves the repo in a
working state.

## Phase 0: Baseline And Instrumentation

### Scope

- add baseline diagnostics logging
- add high-value characterization tests around current behavior
- document current state schema and event coverage gaps

### Why first

This reduces regression risk before structural changes begin.

### Output

- no user-facing architecture change yet
- stronger confidence when later phases move logic around

## Phase 1: Expand App-Server Support

### Scope

- add missing thread methods
- add missing notification mappings
- add explicit initialize capabilities support
- expose `serverRequest/resolved`

### Output

- `appserver` layer is ready to support native-thread-driven flows

## Phase 2: Introduce New Internal State Components

### Scope

- implement `session_registry`
- implement `thread_directory`
- implement `request_registry`
- implement `turn_state`

### Output

- new state components exist alongside legacy store usage

### Temporary compromise

- old store can still back some command outputs during this phase

## Phase 3: Message Pump Cutover

### Scope

- implement `message_pump`
- implement `visibility_classifier`
- move projection responsibilities into normalized event handling plus rendering

### Output

- projector becomes thin or disappears
- message emission becomes deterministic and testable

## Phase 4: Command And UX Cutover

### Scope

- make command layer `cwd`-first
- switch `/threads` to native thread discovery
- hide `project id` from standard output
- improve thread naming behavior
- move settings and approval control to explicit command families
- tune message wording for append-only channel readability
- add batch approval UX for multiple tickets
- add commentary/tool-call visibility commands

### Output

- user-facing surface matches the refactor goal

## Phase 5: Remove Legacy Store Weight

### Scope

- stop relying on bridge-owned project registry as primary source
- remove duplicate thread registries no longer needed
- simplify compatibility layers

### Output

- the architecture becomes meaningfully simpler, not just more layered

## 7. Compatibility Strategy

Large refactors fail when they force all consumers to switch at once.

The compatibility strategy here should be:

1. preserve transport and API entrypoints
2. preserve command names initially
3. preserve immediate-versus-async message classes initially
4. change internals behind those contracts first
5. only then simplify user wording and deprecate legacy commands

This means existing webhook and QQ integrations should survive most of the
refactor unchanged.

## 8. State Migration Strategy

The current store is monolithic. The migration should avoid a flag day.

### Recommended sequence

1. Add a loader that can read both legacy and new state.
2. Introduce new registries with explicit serializers.
3. Mirror legacy state into new records where practical during transition.
4. Cut read paths over one by one:
   - pending requests
   - active thread binding
   - turn activity
   - thread browsing metadata
5. Remove legacy persistence paths after tests prove parity.

### Important rule

Native thread identity should be treated as durable even if local state is
missing or outdated.

## 9. Failure Modes To Design For

The refactor should treat these as explicit cases, not edge cases.

### 9.1 Stale bound thread

Symptoms:

- local binding exists
- `thread/read` or `thread/resume` fails

Expected behavior:

- mark stale
- tell the user
- offer explicit recovery path
- do not silently create a replacement thread

### 9.2 Request resolved elsewhere

Symptoms:

- bridge thinks a ticket is pending
- native request was resolved from another client/surface

Expected behavior:

- `serverRequest/resolved` clears local pending state
- IM-facing ticket becomes closed even if this bridge did not submit the reply

### 9.3 Unknown future notification type

Expected behavior:

- no crash
- state remains consistent
- optional verbose logging

### 9.4 Interrupted turn with late-arriving deltas

Expected behavior:

- stale-turn suppression in message pump
- no user-visible output from superseded turn unless explicitly desired in
  verbose diagnostics mode

## 10. Architecture Review Checklist

Every significant refactor PR should be checked against this list:

1. Does this reduce bridge-owned session semantics, or increase them?
2. Does this move logic closer to native thread/turn/item, or further away?
3. Can this be resumed after restart?
4. Does it preserve separation between channels, bridge, and appserver?
5. Is the user-facing vocabulary more `cwd`-first than before?
6. Does it improve or worsen forward compatibility with new App Server events?

## 11. Suggested PR Sequence

This is the order I would actually ship code in.

1. PR 1: appserver typed method expansion plus notification normalization.
2. PR 2: request registry plus `serverRequest/resolved` support.
3. PR 3: thread directory plus native-thread-backed `/threads`.
4. PR 4: turn state machine and message pump skeleton.
5. PR 5: move current projector behavior onto the message pump.
6. PR 6: `cwd`-first command and status rewrite.
7. PR 7: startup diagnostics and restart-resume tests.
8. PR 8: remove legacy project-heavy persistence paths.

Each PR should leave the app runnable.

## 12. Acceptance Criteria For The Full Refactor

The refactor is complete when:

- native thread identity is the center of the bridge
- local state is minimal and understandable
- message emission flows through one deterministic pipeline
- approval and question flows are resolved by native lifecycle, not bridge guesswork
- user-facing commands are `cwd`-first
- approvals, conditions, and status are all operable through commands
- native permission mode switching is available through commands
- multiple tickets can be approved in one command
- final reply is always shown, commentary is toggleable, tool calls are toggleable
- restart/resume is reliable and tested
- unknown future notifications do not threaten system stability

## 13. Recommended First Implementation Slice

If we start immediately, the best first large slice is:

1. Build the native app-server expansion layer.
2. Add request registry with native request resolution support.
3. Add thread directory and switch `/threads` to read native metadata.
4. Add message pump skeleton and use it for terminal precedence only.
5. Add restart/resume integration coverage.

This slice is large enough to matter, but small enough to ship without freezing
the repo for weeks.
