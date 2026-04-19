# IMCodex Roadmap

This file is the compact roadmap for `imcodex`.

It reflects a deliberate change in planning direction:

- We are no longer assuming that the current bridge should be evolved forever.
- We should prefer a simpler, more native architecture over preserving legacy bridge behavior.
- Existing code is now best treated as:
  - a source of learned constraints
  - a source of reusable adapters
  - not a permanent contract

Read this together with:

- `docs/native-redesign-plan.md`
- `docs/next-step-development-plan.md`
- `docs/issue-notes.md`
- `docs/message-contract.md`

## Baseline We Keep

These are assets, not future work:

- QQ and generic webhook channel experience
- native `codex app-server` connectivity
- working deployment packaging and diagnostics
- a good body of tests and failure notes
- clearer codebase boundaries:
  - `imcodex.channels`
  - `imcodex.bridge`
  - `imcodex.appserver`

## Current Strategy

The next milestone should be a native-first redesign, not another layer of patches.

That means:

- reuse native Codex capabilities wherever they already solve the problem well
- align more closely with native Codex thread, turn, approval, and sandbox semantics
- shrink bridge-owned state to the minimum required for channel mapping
- replace legacy bridge behaviors that exist only because earlier architecture was incomplete
- accept state migration breakage if the replacement model is clearly better

## Done So Far

- native permission profiles now map onto native app-server policy inputs
- native thread metadata is persisted and surfaced:
  - `name`
  - `path`
  - `status`
- `/threads` and `/thread read` now prefer native Codex thread queries
- runtime session routing now prefers a dedicated session index over store scans
- the message pump now suppresses repeated turn progress within a turn and gives
  final answers terminal precedence
- new conversations require an explicit `cwd`
- `/thread attach` can resume a native thread before a `cwd` is preselected
- runtime session start no longer falls back to legacy `project` alias state
- stale native thread bindings now surface `/recover` instead of silently
  replacing the thread
- the main user-facing vocabulary is now `cwd`, `thread`, `turn`, and `ticket`

## Now: Remove The Last Legacy State And Routing Paths

### 0. Message Delivery And Runtime Cleanup

- Add a short-window inbound deduplication layer for IM delivery.
- Deduplication should key off conversation plus content identity and a tight
  time window, not only raw upstream `message_id`.
- Keep raw upstream message identifiers in observability so duplicate-delivery
  root causes can still be diagnosed later.
- Add a native-first steer delivery mode:
  - when a regular turn is in progress, ordinary user text should prefer native
    `turn/steer`
  - ordinary text should not implicitly interrupt a running turn
  - `/stop` remains the explicit interrupt path
- Keep approval handling separate from running-turn steer semantics:
  - pending approvals still resolve through `/approve`, `/deny`, `/cancel`
  - plain text during pending approvals should continue the explicit
    approval-resolution behavior already chosen for IM
- Finish collapsing user-visible runtime modes down to the dedicated-core
  architecture:
  - remove or hide `shared`, `auto`, and `spawned-stdio` from normal user
    paths
  - fix health/status wording so dedicated websocket core is reported
    accurately
- Add a single development command that starts both the dedicated core and the
  bridge together, similar to a one-shot `dev` entrypoint.
- Investigate and stabilize the "tool call appears to hang until the user sends
  another message" failure mode observed in the local Codex runtime.
- Clarify observability around context pressure / compaction so it is easier to
  tell whether long silent turns are caused by tool execution instability or
  background context compression.
- Add IM-user controls for native reasoning effort / thinking intensity.
- Make the current thinking intensity visible in IM status and thread/session
  summaries so users can tell what mode is active.

### 1. Native Session Identity

- Remove the remaining `project`-heavy fields from primary runtime paths.
- Continue shrinking runtime identity to:
  - `threadId`
  - `selected_cwd`
  - native thread metadata when it materially helps recovery
- Replace the remaining `known_thread_ids` and store-scan routing fallbacks with
  runtime-index or native-query paths.

### 2. Native Permission Model

- Keep bridge-level auto-approve out of the design.
- Focus the next round on making native permission profiles clearer in the IM
  surface and docs, not on inventing bridge-owned policy logic.

### 3. Native Message Pump

- Continue turning the outbound queue into a real turn-aware message pump.
- The unit of ordering should be conversation plus turn lifecycle, not just a
  flat message stream.
- Support:
  - throttling
  - deduplication
  - stale-turn suppression
  - partial progress delivery
  - final-result precedence rules

### 4. Tool Visibility Model

- Make tool visibility configurable by stable user-facing categories rather than
  raw protocol events.
- Keep low-value protocol chatter and token deltas out of the main chat flow.
- Add IM-user controls for message visibility so users can choose how much
  progress, tool activity, and protocol chatter appears in chat.
- Make the active message-visibility mode visible from IM-facing status/help
  surfaces.

## Next: Validate Native Continuity On Real Surfaces

### 5. Minimal Persistent State

- Verify that the reduced state model still holds up with:
  - restart -> same conversation
  - IM -> external native thread
  - external native thread -> IM
- Do not preserve extra legacy bridge state if it complicates the model.

### 6. Cross-Surface Continuity

- Validate continuity with real Codex CLI and Desktop-native threads.
- Clarify what roles are played by:
  - `threadId`
  - `cwd`
  - `thread.path`
  - persisted history
- Keep recovery explicit when the thread is stale, but avoid misclassifying
  ordinary transport failures as stale.

### 7. Finish User-Surface Simplification

- Keep reducing routing chatter and legacy vocabulary.
- Make status, requests, and tool-visibility controls read naturally in IM.

## Later: Harden The New Core

### 8. Operability

- Windows service behavior
- stronger logs and health checks
- more explicit runtime diagnostics

### 9. Real-Flow Confidence

- end-to-end tests around the redesigned message pump
- end-to-end tests around native permission modes
- attach/resume scenarios across restart and cross-surface continuation

## Ordering Principles

When choosing work, prefer this order:

1. More native Codex semantics
2. Less bridge-owned complexity
3. Better IM usability
4. Better Windows operability
5. More confidence via end-to-end scenarios

If a change preserves old bridge complexity without improving one of those five things, it is probably the wrong next move.
