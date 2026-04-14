# IMCodex Native Thin Bridge Plan

This document defines the target end-state for `imcodex` as a thin bridge
between an IM channel and the local Codex CLI/App Server.

It is intentionally decision-complete. The goal is to remove ambiguity about
what state belongs to native Codex, what state still belongs to the bridge, and
which integration points are allowed.

This document should be read together with:

- `docs/native-app-server-architecture.md`
- `docs/native-refactor-master-plan.md`
- `docs/im-native-channel-spec.md`

## 1. Thesis

`imcodex` should not behave like a second session platform.

Its role is narrower:

- accept IM messages and commands
- translate them into native Codex operations
- project native Codex output back into the IM channel
- retain only the minimum mapping state that native Codex itself cannot know

Native Codex is the only source of truth for execution and durable session
state.

## 2. Architectural Decisions

The following decisions are mandatory and are not left to the implementer.

### 2.1 Native first-class model

The native model is:

- `cwd`
- `thread`
- `turn`
- `item`
- `request`

The bridge MUST align to that model and MUST NOT reintroduce a bridge-owned
workflow model as the primary session abstraction.

### 2.2 No bridge-owned project entity

`project` is not a first-class bridge concept.

Rules:

- user-facing output MUST use `cwd` as the primary workspace concept
- bridge persistence MUST NOT store or restore a separate project registry
- trust/project metadata managed by native Codex remains native-owned

If the product needs to describe workspace continuity, it should do so through:

- `cwd`
- native thread metadata
- native git metadata when available

### 2.3 Native Codex owns execution and durable state

The following are native-owned and MUST NOT be persistently modeled by the
bridge:

- thread metadata
- turn lifecycle state
- approval lifecycle truth
- thread names
- thread status
- thread history
- rollout history
- sandbox and approval mode truth
- project/trust metadata

The bridge MAY temporarily project some of these into outbound messages or
runtime memory, but MUST NOT treat local persistence as authoritative.

### 2.4 Native store files are read-only integration surfaces

The native Codex files below are implementation data stores, not bridge-owned
control-plane state:

- `~/.codex/state_5.sqlite`
- `~/.codex/session_index.jsonl`
- `~/.codex/sessions/*.jsonl`

Their role in `imcodex` is strictly limited:

- `state_5.sqlite` is a native local index database, not a write interface
- `session_index.jsonl` is a native recent-thread index, not a bridge queue
- `sessions/*.jsonl` are native rollout/event logs, not a bridge control plane

The bridge MAY read them for discovery, recovery, or diagnostics.
The bridge MUST NOT write them directly.

### 2.5 Native writes go through Codex interfaces only

All state-changing operations MUST go through native Codex interfaces, with the
App Server as the default write surface.

This includes:

- thread creation
- thread resume or attach validation
- turn start and steer
- approvals and denials
- structured user input replies
- thread metadata changes when supported natively

The Codex CLI is treated as the host/runtime entrypoint, not as permission to
write native sqlite or native jsonl files directly.

## 3. Target Persistent State

The bridge persistent state must be reduced to two structures.

### 3.1 ConversationBinding

```python
@dataclass(slots=True)
class ConversationBinding:
    channel_id: str
    conversation_id: str
    thread_id: str | None
    bootstrap_cwd: str | None
```

Semantics:

- `thread_id` is the currently attached native thread for this IM conversation
- `bootstrap_cwd` exists only before a thread is bound and is used to start the
  first native thread

Rules:

- once a native thread is attached, `cwd` is derived from native thread
  metadata instead of the persisted binding
- `bootstrap_cwd` MAY remain persisted for convenience, but it is no longer the
  authoritative workspace once `thread_id` exists

### 3.2 PendingRequestBinding

```python
@dataclass(slots=True)
class PendingRequestBinding:
    channel_id: str
    conversation_id: str
    ticket_id: str
    request_id: str
    thread_id: str
    turn_id: str | None
    kind: str
```

Semantics:

- this is only a routing and user-handle mapping record
- it exists so the IM user can refer to native pending work by a short ticket id

Rules:

- request validity and terminal resolution are native truths
- local persistence MUST NOT invent independent pending-state truth once native
  resolution has been observed

### 3.3 Explicit removals from persistence

The following fields or structures must be removed from long-term bridge state
or treated as non-persistent runtime-only data:

- full local `ThreadRecord` directory
- `thread_order`
- `thread_first_user_messages`
- `thread_active_turns`
- `last_seen_thread_name`
- `last_seen_thread_path`
- `last_seen_thread_status`
- `active_turn_id`
- `active_turn_status`
- `selected_model`
- `permission_profile`
- `visibility_profile`
- `show_commentary`
- `show_toolcalls`

The following legacy compatibility fields must be deleted from the persisted
schema and from future compatibility logic:

- `project_id`
- `active_project_id`
- `known_thread_ids`

Runtime-only message pump or aggregation state MAY still exist, but it MUST NOT
be written into the bridge state file.

## 4. Read And Write Paths

This section defines the required bridge behavior.

### 4.1 Creating new work

Flow:

1. IM message arrives.
2. Bridge resolves whether the conversation already has a `thread_id`.
3. If not, bridge requires `bootstrap_cwd`.
4. Bridge calls native `thread/start` using that `bootstrap_cwd`.
5. Bridge stores the returned `thread_id` in `ConversationBinding`.
6. All subsequent work for that conversation uses the native thread.

Rules:

- the bridge MUST NOT create a local synthetic thread record as the primary
  state
- the bridge MUST NOT maintain a second authoritative thread directory

### 4.2 Attaching an existing thread

Flow:

1. User sends `/thread attach <thread-id>`.
2. Bridge calls native `thread/read` to validate that thread.
3. If valid, bridge stores the `thread_id` binding.
4. Any local derived thread or turn cache for that conversation is cleared.

Rules:

- attach success is defined by native validation, not by local history
- attach MUST NOT silently synthesize missing native metadata from a local cache

### 4.3 Status and discovery queries

The commands below MUST prefer native App Server data:

- `/status`
- `/threads`
- `/thread read`

Allowed fallback:

- if native App Server is temporarily unavailable or if discovery needs
  supplemental recovery data, the bridge MAY perform read-only inspection of
  native store files

Fallback rules:

- read-only native-store inspection MUST supplement discovery only
- it MUST NOT become the primary write path
- it MUST NOT override a successful App Server response

### 4.4 Approvals and question replies

The bridge stores only ticket mappings.

Flow:

1. Native request arrives.
2. Bridge creates or restores a conversation-local `ticket_id`.
3. Bridge persists the `request_id -> ticket_id` mapping.
4. User replies with `/approve`, `/deny`, or other ticket-driven commands.
5. Bridge forwards the resolution to the native interface.
6. Native resolution events determine whether the request is still pending.

Rules:

- local pending state is a routing helper, not the source of truth
- if native `serverRequest/resolved` or equivalent terminal request state is
  observed, the local mapping must be cleared

### 4.5 Restart recovery

Flow:

1. Bridge reloads persisted `ConversationBinding` records.
2. For each bound `thread_id`, bridge calls native `thread/read` on demand or
   during recovery validation.
3. If the native thread exists, the binding remains valid.
4. If the native thread does not exist, the bridge marks the conversation as
   unbound and surfaces explicit recovery guidance.

Rules:

- there is no silent thread replacement
- there is no local recreation of missing native thread metadata
- recovery failure must be visible to the IM user

## 5. Refactor Direction

This section defines how the codebase should converge toward the target model.

### 5.1 `store.py`

`store.py` should be rewritten as a minimal mapping store.

Its long-term responsibilities should be limited to:

- `ConversationBinding` persistence
- `PendingRequestBinding` persistence
- one-shot compatibility migration from older state files

It should no longer own:

- thread directory persistence
- turn state persistence
- thread status cache
- user-facing thread metadata cache
- permission or visibility policy persistence

### 5.2 `bridge/thread_directory.py`

`bridge/thread_directory.py` should be downgraded from a persisted local
directory to a thin native-query helper or removed entirely.

Acceptable end states:

- delete the module and query native sources directly
- retain it only as a read-only adapter over native thread discovery

Unacceptable end state:

- keeping it as a second authoritative thread catalog

### 5.3 `bridge/session_registry.py`

`bridge/session_registry.py` should manage only conversation-to-thread routing.

It should no longer be responsible for:

- migrating stored thread runtime state between conversations
- acting as a shadow turn-state owner
- maintaining local truth for thread status or naming

### 5.4 Projection and command behavior

The command surface remains, but its meaning changes:

- commands operate native `cwd`, thread, turn, and request semantics
- commands do not operate a bridge-owned session platform

Thread name, thread status, cwd, approval mode, and turn state should be:

- projected from native notifications
- queried from native APIs
- treated as runtime display data only

They should not become persisted bridge truth.

## 6. Compatibility And Migration

Old bridge state files may still exist during the transition.

Migration rules:

- only the minimum conversation binding information should be preserved
- old thread catalogs and derived fields must be ignored
- legacy `project_id`, `active_project_id`, and `known_thread_ids` must not be
  carried forward
- after the first save in the new format, obsolete fields should naturally
  disappear

This is intentionally a reducing migration, not a compatibility-preserve-forever
design.

## 7. Acceptance Tests

The following scenarios are required acceptance coverage for the refactor.

### 7.1 New conversation bootstrap

- user sets `/cwd` only
- first natural-language message creates a native thread
- returned `thread_id` is persisted in `ConversationBinding`

### 7.2 Native-first status queries

- user attaches a thread with `/thread attach`
- `/status` and `/threads` reflect native App Server data
- bridge does not answer from stale local thread cache

### 7.3 Restart recovery

- bridge restarts with existing conversation-thread bindings
- valid native threads are reattached successfully
- missing native threads cause explicit unbound recovery state rather than
  silent replacement

### 7.4 Approval recovery

- a pending approval survives bridge restart through `request_id` mapping
- once the native side resolves the request, the local ticket mapping is
  cleared
- stale resolved tickets are not shown as pending

### 7.5 Cross-surface thread continuity

- local bridge thread directory is absent or empty
- a thread created by native CLI/Desktop can still be discovered and attached
- bridge continuity depends on native thread identity, not on bridge-owned
  thread history

### 7.6 Native store access boundaries

- tests confirm native-store inspection is read-only
- bridge never writes `state_5.sqlite`
- bridge never writes `session_index.jsonl`
- bridge never writes `sessions/*.jsonl`

### 7.7 Old state migration

- loading an old bridge state preserves only minimal binding information
- obsolete fields are ignored
- saving the migrated state removes those obsolete fields from the new file

## 8. Assumptions

- the document path is fixed at `docs/native-thin-bridge-plan.md`
- existing native-first documents remain in place; this document is the
  implementation baseline for the final thin-bridge reduction
- small runtime-only in-memory state for message pump ordering, throttling, and
  notification coalescing is still allowed
- runtime-only state must not be written into repo-tracked or bridge-managed
  persistent state files
- native App Server is the default write interface
- direct mutation of native sqlite or native session jsonl files is not allowed
