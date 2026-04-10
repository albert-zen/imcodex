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

## Now: Design The New Native Core

### 1. Native Session Identity

- Define the minimum identity needed for continuity:
  - `threadId`
  - `cwd`
  - native `thread.path` when it materially matters
  - persisted history settings when they materially affect resume/read fidelity
- Clarify what must be bridge-owned versus Codex-owned.
- Prefer native discovery and recovery paths such as `thread/list`, `thread/read`, and `thread/resume` over bridge-invented registries when possible.
- Define restart, attach, and resume semantics around native Codex behavior first.

### 2. Native Permission Model

- Stop treating bridge-level auto-approve as the long-term answer.
- Rebuild permission handling around native Codex:
  - `approval_policy`
  - `sandbox_policy`
  - native permission profiles or modes
- Decide what permission choices the IM surface should expose to users.

### 3. Native Message Pump

- Design a queue or message-pump model for asynchronous chat delivery.
- The unit of ordering should not just be "messages", but conversation plus turn lifecycle.
- Support:
  - throttling
  - deduplication
  - stale-turn suppression
  - partial progress delivery
  - final-result precedence rules

### 4. Tool Visibility Model

- Make tool visibility configurable by user-facing category rather than raw protocol event.
- Decide the stable categories worth exposing in chat.
- Keep low-value protocol chatter and token deltas out of the main chat flow.

## Next: Rebuild The Bridge Around That Design

### 5. Minimal Persistent State

- Rebuild persisted state around the new native model.
- Keep only the minimum bridge-owned information:
  - channel/conversation binding
  - selected `cwd`
  - selected native thread
  - user-facing display preferences
- Do not preserve legacy bridge state just for compatibility if it complicates the model.

### 6. Replace Legacy Approval And Routing Behavior

- Remove bridge-specific approval shortcuts that duplicate native Codex behavior.
- Remove legacy routing chatter that only exists to explain internal bridge choices.
- Make normal chat feel like a native Codex surface rather than a wrapper.

### 7. Rebuild Resume / Attach Around Native Semantics

- Treat `thread/resume` and attach as primary workflows, not optional extras.
- Make cross-surface continuation explicit:
  - IM -> CLI/Desktop
  - CLI/Desktop -> IM
  - restart -> same conversation

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
