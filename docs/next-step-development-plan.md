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

This should be redesigned from first principles against native `thread/resume`
behavior instead of by growing the current store model.

### 4.2 Permission model

The bridge-level auto-approve path was useful as a temporary product escape
hatch, but it should not be the permanent architecture.

We should redesign around native Codex concepts:

- `approval_policy`
- `sandbox_policy`
- native permission modes and profiles

### 4.3 Message pump

Current message projection works, but it is still the result of incremental
fixes around progress, final answers, and async ordering.

The better long-term model is a real per-conversation/per-turn message pump that
explicitly handles:

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

## 6. Recommended Execution Order

The next cycle should follow this sequence.

## Phase 1: Design The Native Core

### Goal

Freeze the right architecture before more feature work accumulates on top of the
wrong one.

### Deliverables

- a session identity design
- a permission model design
- a message-pump design
- a tool-visibility design
- a minimal persisted-state design

### Why first

Without these decisions, future work on portability, progress visibility, and
permissions will keep re-litigating the same assumptions.

## Phase 2: Rebuild The Core Around Native Semantics

### Goal

Replace the most legacy-shaped parts of the current bridge with a smaller,
clearer native-first core.

### Scope

- rebuild state around native session continuity
- replace bridge-owned approval shortcuts
- introduce the new message pump
- make tool visibility configurable by category
- keep channel adapters and app-server transport where possible

### Why second

This is the first phase where code churn is worth paying for.
By then, the design should already justify what gets kept and what gets thrown away.

## Phase 3: Reattach Product Features To The New Core

### Goal

Reconnect the user-facing experience on top of the rebuilt kernel.

### Scope

- `cwd`-first terminology
- human-readable thread labels
- attach and resume workflows
- cleaner status output
- reduced chat noise

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

If we start coding after this planning step, the best next slice is not a user
feature. It is a redesign slice:

1. write the session identity design
2. write the native permission model design
3. write the message-pump design
4. decide what old store data can be intentionally dropped
5. only then start the rebuild

This is slower than shipping one more patch, but faster than hardening the wrong
core for another cycle.

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
