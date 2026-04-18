# ADR 0001: Native-First Thin Bridge Architecture

- Status: Accepted
- Date: 2026-04-14

## Context

`imcodex` is intended to connect IM channels to a locally running Codex instance.

The long-term product goal is not to create a second agent framework on top of Codex. Instead, `imcodex` should behave as a thin transport and projection layer over native Codex capabilities.

This ADR deliberately does **not** treat the current implementation as a constraint. We are allowed to rewrite the bridge if the new architecture is simpler, more native, and easier to operate.

The key design pressure is:

- native Codex already owns most of the real session state
- IM platforms introduce a small amount of platform-specific state and UX requirements
- previous bridge designs risked duplicating native state in a second local system

We want to avoid rebuilding:

- a second project system
- a second thread directory
- a second turn lifecycle
- a second request identity model
- a second permission system
- a second default model system

## Decision

`imcodex` will adopt a **native-first thin bridge** architecture.

The system will be organized into three layers:

1. `channels`
   Platform-specific ingress and egress.

2. `bridge`
   IM conversation binding, IM-only preferences, and event projection.

3. `appserver`
   Native Codex integration and config operations.

The bridge will be rewritten, where necessary, so that native Codex is the source of truth for session state and execution behavior.

## Native-First Ownership Model

### Native Codex owns

The following are treated as native Codex state and must not be reimplemented as bridge truth:

- thread lifecycle
  - start
  - resume
  - fork
  - read
  - list
  - archive
  - unarchive
  - naming
  - status
  - history
  - path
- turn lifecycle
  - start
  - steer
  - interrupt
  - completion
- item lifecycle
  - agent output
  - tool progress
  - command/file/reasoning items
- request lifecycle
  - approvals
  - request-user-input
  - native request identity
- working directory continuity once a native thread exists
- model continuity at the native thread level
- reasoning effort continuity at the native thread level
- execution policy truth
  - approval policy
  - sandbox policy
  - approvals reviewer
- global Codex configuration
  - model
  - approval defaults
  - sandbox defaults
  - feature flags

### IM bridge owns

The following remain bridge-owned because native Codex cannot know them:

- `channel_id + conversation_id -> native thread_id`
- `bootstrap_cwd`
  - only before the first native thread exists
- IM-only visibility preferences
  - visibility profile
  - show commentary
  - show toolcalls
- channel-specific reply context
  - message ids
  - reply anchors
  - channel routing metadata
- the minimal mapping needed to route native requests back into an IM conversation

## Architecture

### 1. Channels layer

Responsibilities:

- receive messages from IM platforms
- normalize inbound data into a shared message shape
- send projected outbound messages back to the platform
- own platform-specific reply metadata

Examples:

- QQ websocket ingestion
- webhook ingress
- outbound reply formatting for a given platform

Non-responsibilities:

- thread lifecycle truth
- turn lifecycle truth
- permission truth
- model truth

### 2. Bridge layer

Responsibilities:

- map an IM conversation to a native Codex thread
- hold IM-only preferences
- parse slash commands into native operations
- project native events into IM-visible messages
- maintain a runtime session index for active routing

Non-responsibilities:

- owning a second session directory
- persisting a local thread catalog as primary truth
- inventing synthetic request identity when native request ids are sufficient
- implementing a bridge-owned approval engine
- implementing a bridge-owned default model system

### 3. Appserver layer

Responsibilities:

- speak native Codex app-server protocol
- expose native thread and turn operations
- expose native config operations
- surface native request and event streams

This layer should be the only place that knows protocol details such as:

- `thread/start`
- `thread/resume`
- `thread/read`
- `thread/list`
- `turn/start`
- `turn/steer`
- `turn/interrupt`
- `config/read`
- `config/value/write`
- `config/batchWrite`

## State Model

The target persisted state is intentionally small.

### Persisted bridge state

The persisted bridge state should contain only:

- conversation binding
  - channel id
  - conversation id
  - current native thread id
  - bootstrap cwd, if no native thread exists yet
- IM-only visibility preferences
- channel-specific reply context if the platform requires it
- minimal native request routing data

### Runtime-only state

The following should be runtime-only and rebuildable:

- active turn routing
- runtime session index
- in-memory message pump queues
- transient native request tracking helpers

### Explicitly not persisted by the bridge

The bridge should not persist:

- a project registry
- per-conversation project ids
- a local thread directory as primary truth
- local turn truth as primary truth
- synthetic ticket ids as a required request model
- per-conversation default model overrides
- per-conversation permission truth

## Command Model

Commands should operate as thin translations to native Codex behavior.

Primary commands:

- `/cwd <path>`
- `/status`
- `/threads`
- `/thread attach <thread-id>`
- `/thread read`
- `/new`
- `/stop`
- `/approve [request-id-or-prefix]`
- `/deny [request-id-or-prefix]`
- `/cancel [request-id-or-prefix]`
- `/answer [request-id-or-prefix] key=value ...`
- `/view ...`
- `/show commentary|toolcalls`
- `/hide commentary|toolcalls`

### Model control

`/model` is allowed, but it must not imply bridge-owned long-term model state.

Preferred semantics:

- one-shot native override
- or native Codex config write

Rejected semantics:

- storing a conversation-level default model in bridge state

### Permission control

Permission behavior should map to native Codex settings.

Preferred semantics:

- use native config for global defaults
- use native request-level overrides only when necessary

Rejected semantics:

- bridge-owned long-term approval truth
- bridge-owned auto-approve as a separate authority model

## Request Identity Model

Native request identity is authoritative.

The bridge should prefer native `requestId` over synthetic ticket systems.

If IM usability requires a compact handle, it may exist only as a presentation aid, not as the underlying source of truth.

Preferred interaction model:

- `/approve`, `/deny`, and `/cancel` without an id act on all pending approvals in the current conversation
- when multiple approvals are pending, the user may target a native request id or a short prefix derived from it
- a normal text message while approvals are pending should cancel them before continuing with the new input

## Message Pump

The bridge must include a turn-aware outbound message pump.

The message pump is justified because IM platforms require projection, throttling, and ordering that native Codex does not know about.

The message pump may own:

- ordering
- throttling
- stale-turn suppression
- final-result precedence
- visibility filtering

The message pump must not become a second source of truth for turn state.

## Migration Strategy

This ADR explicitly allows a rewrite-oriented migration.

Rules:

- old bridge state may be discarded or rebuilt
- compatibility layers should be temporary and removed aggressively
- no new feature should be accepted if it deepens the bridge-owned session model
- native Codex capabilities must be preferred whenever they can replace local state

## Consequences

### Positive

- simpler mental model
- clearer ownership boundaries
- better alignment with Codex CLI, app-server, and native session behavior
- less duplicated state
- easier future support for cross-surface continuity

### Negative

- some current compatibility behaviors may be removed
- some UX patterns may need to change to align with native identity
- rewrite effort is acceptable and expected

## Rejected Alternatives

### Alternative 1: Maintain a rich local bridge session model

Rejected because it recreates native Codex state in a second system and increases drift risk.

### Alternative 2: Keep project, thread, request, and permission abstractions as first-class bridge concepts

Rejected because they duplicate native capabilities and make IMCodex harder to reason about.

### Alternative 3: Preserve compatibility at all costs

Rejected because this product is still early and should optimize for a cleaner target architecture rather than preserving every historical bridge behavior.

## Follow-up Design Work

The following design tasks should align with this ADR:

- state schema reduction
- native request id UX
- `/model` semantics using native config or one-shot overrides
- permission controls aligned to native config
- runtime session index design
- message pump design
- cross-surface continuity tests
