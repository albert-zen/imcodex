# IMCodex Next-Step Development Plan

This document updates the execution plan for `imcodex`.

The most important change is strategic:

- We should stop assuming the current bridge is the final kernel.
- We should plan around a native-first redesign.
- Existing code should be reused selectively, not preserved by default.

Read this together with:

- `todo.md` for the compact roadmap
- `docs/native-redesign-plan.md` for the redesign target
- `docs/issue-notes.md` for the constraints we already learned the hard way
- `docs/message-contract.md` for the current bridge contract
- `docs/deployment.md` for current runtime packaging and diagnostics

## 1. Current Planning Assumption

The bridge already proved three things:

1. the channel side is feasible
2. the native `codex app-server` side is feasible
3. the user-facing pain now comes mostly from bridge-owned behavior, not from a lack of raw connectivity

That changes how we should plan.

The next milestone should not be:

- "improve the existing bridge a little more"

The next milestone should be:

- "define and rebuild a more native bridge core with less custom behavior"

## 1.1 What Has Already Landed

The repo is no longer at pure design stage. These native-first slices are
already in the mainline:

- native permission profiles wired into app-server request overrides
- native thread metadata persisted from `thread/start`, `thread/resume`,
  `thread/read`, and `thread/list`
- `/threads` and `/thread read` routed through native backend query paths
- runtime session index routing replacing store scans as the primary route
- `cwd`-first persisted state with legacy project aliases demoted
- message-pump deduplication plus final-answer precedence
- explicit `cwd` required for new conversations and new threads
- `/thread attach` allowed before a working directory is preselected
- runtime session resolution no longer depends on legacy `active_project_id`
- stale thread bindings surface explicit recovery instead of silent replacement
- the main user-facing vocabulary is now `CWD`, `thread`, `turn`, and `ticket`

So the next phase is not "start the redesign".
It is "finish removing the last legacy registries and validate the native model
against real cross-surface workflows".

## 2. What The Current System Is Good For

The current implementation is still valuable, but its role has changed.

It now serves as:

- a working reference implementation
- a source of reusable channel adapters
- a source of reusable app-server transport code
- a source of tests and operational knowledge
- a catalog of failure modes already captured in `docs/issue-notes.md`

It should not automatically be treated as:

- the final state model
- the final approval model
- the final message-pump model
- the final user-facing semantics

## 3. The Main Design Goal

The new core should be closer to Codex native behavior and lighter in its own state.

In practice, that means:

- reuse native Codex capabilities whenever they already solve the problem well
- preserve native Codex thread and turn semantics
- preserve native approval and sandbox semantics
- reduce bridge-owned state to channel mapping plus user-facing presentation
- prefer explicit rebuild over compatibility patches when the old model is wrong

## 4. What We Should Rebuild, Not Patch

These are the areas where continuing to patch the current implementation is
likely the wrong tradeoff.

### 4.1 Session identity model

We still need a cleaner answer to:

- what defines continuity:
  - `threadId`
  - `cwd`
  - persisted history mode
  - session path
- what belongs to Codex state versus bridge state

The current implementation is much closer to this model now, but the remaining
problem is not high-level shape. It is the last legacy fallbacks still living
inside the store and routing helpers.

It should also explicitly consider which native APIs should become primary:

- `thread/list`
- `thread/read`
- `thread/resume`
- `thread.path`
- persisted-history support

### 4.2 Permission model

The bridge-level auto-approve path has already been cut over to native
permission profiles, so the remaining work here is mostly product fit and
documentation rather than architectural cleanup.

We should redesign around native Codex concepts:

- `approval_policy`
- `sandbox_policy`
- native permission modes and profiles

### 4.3 Message pump

Current message projection is meaningfully better than before, but it is still
not a fully mature outbound coordinator.

The remaining work is to keep turning it into a real per-conversation/per-turn
message pump that explicitly handles:

- throttling
- deduplication
- stale-turn suppression
- partial progress
- terminal message precedence

### 4.4 Tool visibility model

Tool visibility should be rebuilt around stable user-facing categories, not
around raw app-server event shapes.

That is a product model decision first, not only a projector tweak.

## 5. What We Should Keep And Reuse

These parts are worth keeping unless the redesign reveals a better replacement:

- channel adapters and outbound sinks
- app-server transport and supervisor code
- deployment scripts and diagnostics baseline
- existing test suites as a starting point
- issue notes and message-contract docs as input constraints

These native capabilities should also be treated as preferred building blocks,
not optional extras:

- native `cwd` handling on thread and turn operations
- native `thread/list` filtering by `cwd`
- native `thread/read` and `thread/resume`
- native approval and sandbox policies
- native thread metadata such as `thread.path`

## 6. Recommended Execution Order

The next cycle should follow this sequence.

## Phase 1: Finish The Remaining Native Core Reduction

### Goal

Freeze the right architecture before more feature work accumulates on top of the
wrong one.

### Deliverables

- remove the remaining project-heavy persisted fields from primary runtime paths
- replace the last store-scan routing fallbacks with native or runtime-index paths
- define the remaining continuity invariants against real native thread behavior
- tighten the remaining message-pump ordering and throttling rules

### Why first

This is the smallest remaining slice that still reduces real architectural
weight. It keeps us from spending the next round polishing a transitional state
model.

## Phase 2: Validate Native Continuity Against Real Surfaces

### Goal

Replace the most legacy-shaped parts of the current bridge with a smaller,
clearer native-first core.

### Scope

- validate CLI/Desktop to IM continuity with native `threadId`
- verify how far `thread.path`, persisted history, and `cwd` actually define continuity
- document recovery rules when the native thread is valid but transport is unstable
- expand high-value mock and real-flow continuity coverage

### Why second

This is the first phase where code churn is worth paying for.
By then, the design should already justify what gets kept and what gets thrown away.

## Phase 3: Finish Product-Surface Simplification

### Goal

Reconnect the user-facing experience on top of the rebuilt kernel.

### Scope

- finish removing low-value routing chatter that still leaks through commands or progress
- make tool visibility category controls more user-comprehensible
- keep thread naming aligned with native name / preview / first-user-message order
- refine status and request surfaces without reintroducing project-first vocabulary

### Why this follows rebuild

These features should sit on top of the final state and message model, not on
top of transitional infrastructure.

## Phase 4: Harden Operability And Confidence

### Goal

Make the rebuilt system easy to trust in practice.

### Scope

- runtime diagnostics and logs
- Windows service behavior
- high-value end-to-end scenarios
- cross-surface continuation tests

## 7. Concrete Planning Guidance For The Next Implementation Round

The best next slices are now implementation slices, not more speculative
planning:

1. demote the persisted `project` layer into a compatibility alias
2. replace `known_thread_ids` / `find_binding_for_thread()` with a thinner
   native session index
3. keep strengthening the message pump:
   - ordering
   - throttling
   - stale-turn suppression
4. continue shrinking the monolithic store to the minimum bridge-owned state

Additional design notes are still useful, but they should only exist to unblock
those concrete refactor slices.

## 8. Architecture Decision Heuristics

When deciding whether to preserve or replace an existing behavior, prefer these
rules:

### Keep it if

- it already matches native Codex semantics
- it is a thin adapter around channel or transport reality
- it reduces operational risk without adding bridge-specific logic

### Replace it if

- it exists mainly to paper over an earlier bridge limitation
- it duplicates a native Codex capability
- it adds bridge-owned state that users do not actually need
- it makes cross-surface continuity harder to reason about

## 9. Definition Of A Good Next Milestone

The next milestone is good if:

- the planned core is more native than the current one
- the bridge owns less policy and less identity than it does today
- the future IM experience becomes easier to explain
- we are positioned to discard old state and old approval shortcuts deliberately
