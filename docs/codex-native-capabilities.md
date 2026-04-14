# Codex Native Capabilities Matrix

This document records which capabilities are already owned by native Codex and which ones should remain in `imcodex`.

Primary references:

- `codex-upstream/codex-rs/app-server/README.md`
- `codex-upstream/codex-rs/core/src/config/service_tests.rs`
- local `codex --help`

## Design Principle

`imcodex` should act as a thin bridge.

- If native Codex already has a stable source of truth for a capability, prefer native Codex.
- `imcodex` should persist only IM-specific state that native Codex cannot know.
- `imcodex` should not rebuild a second session platform on top of native thread/turn/item state.

## Native Capabilities

### Thread and session lifecycle

Native Codex already owns:

- `thread/start`
- `thread/resume`
- `thread/fork`
- `thread/list`
- `thread/read`
- `thread/archive`
- `thread/unarchive`
- `thread/name/set`
- `thread/metadata/update`
- `thread/rollback`
- `thread/unsubscribe`
- `thread/loaded/list`

Native notifications include:

- `thread/started`
- `thread/status/changed`
- `thread/name/updated`
- `thread/archived`
- `thread/unarchived`
- `thread/closed`

Implication for `imcodex`:

- Native Codex should be the source of truth for thread existence, thread history, thread name, thread path, and thread status.
- `imcodex` should not maintain a competing local thread directory as the primary truth.

### Working directory (`cwd`)

Native Codex already supports `cwd` in:

- `thread/start`
- `turn/start`
- `thread/list` filtering

When a thread already exists, its native session context should be treated as authoritative.

Implication for `imcodex`:

- `bootstrap_cwd` is only needed before the first native thread exists.
- Once a conversation is bound to a native thread, `imcodex` should prefer native thread continuity over local project abstractions.

### Turn lifecycle and steering

Native Codex already owns:

- `turn/start`
- `turn/steer`
- `turn/interrupt`
- `turn/started`
- `turn/completed`
- `turn/plan/updated`
- `turn/diff/updated`

Implication for `imcodex`:

- Steer behavior should use native `turn/steer`.
- Turn completion, interruption, and stale-turn handling should follow native turn identity.

### Streaming items and agent output

Native Codex already streams:

- `item/started`
- `item/completed`
- `item/agentMessage/delta`
- tool-related progress items
- command/file-change/reasoning items

Implication for `imcodex`:

- `imcodex` should consume native item events and project them to IM.
- The bridge may own an outbound message pump, but not the underlying item truth.

### Native approvals and user-input requests

Native Codex already owns:

- approval requests
- `tool/requestUserInput`
- request resolution lifecycle
- `serverRequest/resolved`

Implication for `imcodex`:

- Native `requestId` is the real identity.
- `imcodex` should only map request ids to IM conversations and render them in an IM-friendly way.
- Synthetic request systems should be avoided unless they exist purely as UI handles.

### Model and reasoning continuity

Native Codex already supports:

- explicit `model` override on `thread/start` and `turn/start`
- `model/list`
- persisted `model` and `reasoningEffort` continuity on `thread/resume`

The app-server README explicitly states that `thread/resume` uses the latest persisted `model` and `reasoningEffort` associated with the thread unless explicit overrides are supplied.

Implication for `imcodex`:

- Thread-level model continuity should remain native.
- `imcodex` does not need to persist a conversation-level default model override unless there is a deliberate product decision to support one.

### Native config management

Native Codex already supports:

- `config/read`
- `config/value/write`
- `config/batchWrite`
- `config/mcpServer/reload`
- `configRequirements/read`

The native config layer already covers values such as:

- `model`
- approval policy
- sandbox policy
- feature flags

Implication for `imcodex`:

- Global defaults should prefer native config rather than bridge-owned long-term state.
- Commands like `/model` should prefer writing native Codex config or using native one-shot overrides rather than inventing bridge-specific model state.

### Approval and sandbox execution

Native Codex already supports:

- `approvalPolicy`
- `sandbox`
- `approvalsReviewer`

These can be passed to native thread/turn APIs and can also come from native config.

Implication for `imcodex`:

- The bridge should not become the source of truth for execution permissions.
- If `imcodex` exposes permission controls, they should map to native Codex settings rather than implementing a separate approval engine.

### Filesystem and command APIs

Native Codex already exposes:

- `command/exec`
- `command/exec/write`
- `command/exec/resize`
- `command/exec/terminate`
- `command/exec/outputDelta`
- `fs/readFile`
- `fs/writeFile`
- `fs/createDirectory`
- `fs/getMetadata`
- `fs/readDirectory`
- `fs/remove`
- `fs/copy`
- `fs/watch`
- `fs/unwatch`
- `fs/changed`

Implication for `imcodex`:

- The bridge does not need to invent a separate execution or filesystem abstraction.
- The bridge only decides what to expose in IM and how to present the results.

### Native skills, apps, plugins, and MCP

Native Codex already supports:

- `skills/list`
- `skills/config/write`
- `skills/changed`
- `app/list`
- `plugin/list`
- `plugin/read`
- `plugin/install`
- `plugin/uninstall`
- `mcpServerStatus/list`
- `mcpServer/oauth/login`
- `experimentalFeature/list`
- `experimentalFeature/enablement/set`
- `collaborationMode/list`

Implication for `imcodex`:

- `imcodex` should not duplicate plugin/skills/app registries.
- The bridge should forward or surface native capabilities where useful.

## CLI-Native Operations

The local Codex CLI supports:

- `exec`
- `review`
- `resume`
- `fork`
- `app-server`
- `mcp`
- `mcp-server`
- `apply`
- `cloud`
- `features`

The CLI also supports native runtime overrides:

- `-m, --model`
- `-c, --config key=value`
- `-p, --profile`
- `-s, --sandbox`
- `-a, --ask-for-approval`
- `-C, --cd`

Important distinction:

- CLI supports one-shot runtime overrides directly.
- Persistent default changes are more naturally represented by native config writes than by bridge-owned state.

## What `imcodex` Should Persist

Only persist state that native Codex cannot know.

Recommended bridge-owned persisted state:

- `channel_id + conversation_id -> thread_id`
- `bootstrap_cwd` before a native thread exists
- IM visibility preferences
  - `visibility_profile`
  - `show_commentary`
  - `show_toolcalls`
- channel-specific reply context if required by a platform
- minimal pending request routing:
  - native `requestId`
  - conversation identity
  - optional thread/turn linkage for projection

## What `imcodex` Should Avoid Persisting

Avoid bridge-owned state for:

- project registries
- synthetic thread directories as primary truth
- local turn truth as primary truth
- conversation-level default model state unless explicitly required
- long-term approval truth that duplicates native Codex execution settings

## Practical Architecture Guidance

The thinnest stable architecture is:

1. `channels`
   Responsible for IM ingress/egress and platform-specific reply metadata.

2. `bridge`
   Responsible for:
   - conversation-to-native-thread binding
   - IM visibility preferences
   - native event projection into IM

3. `appserver`
   Responsible for native Codex APIs and native config operations.

In short:

- native Codex owns session truth
- `imcodex` owns IM projection and IM-only preferences
- channel adapters own platform transport details
