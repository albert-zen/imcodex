# AGENTS.md

This file defines the decision and development principles for contributors working on `imcodex`.

`imcodex` is a thin IM bridge over native Codex. We are not building a second agent framework, a second thread runtime, or a second policy engine.

## Goal

- Keep `imcodex` native-first, thin, and operable.
- Prefer clear ownership boundaries over convenience abstractions.
- Prefer predictable recovery and diagnosis over hidden local magic.

## Decision Priorities

- Native Codex semantics come first when there is overlap in responsibility.
- Bridge-owned complexity must stay intentionally small.
- Local state is a last resort, not a default design tool.
- Operational simplicity is a product feature. If a design is hard to observe or recover, it is probably too complicated.

## Source Of Truth

Native Codex source code and native protocol behavior are the source of truth
for execution semantics in `imcodex`.

That means:

- first check whether native Codex already implements the behavior
- if it does, integrate with that native capability directly
- only add bridge-owned state when native Codex does not expose the required
  behavior and the IM bridge still needs it

In practice, native Codex is the source of truth for:

- thread lifecycle
- turn lifecycle
- request identity
- model continuity
- reasoning effort
- permission and sandbox behavior
- native request and approval state

The bridge may own only IM-specific concerns such as:

- channel and conversation bindings
- bootstrap context before a native thread exists
- channel reply context
- IM-only visibility preferences
- minimal request routing needed to complete native flows

## Layer Boundaries

- `channels` adapt external transports and should stay transport-focused.
- `bridge` translates IM intent into native requests and projects native events back into IM-safe messages.
- `appserver` owns Codex protocol integration and transport details.
- `composition` wires the system together but is not a business layer.

Dependency direction is one-way:

- `channels` must not depend on `bridge` or `appserver`
- `bridge` may depend on `appserver`, but not on `channels`
- `appserver` must not depend on `bridge` or `channels`

## State Discipline

- Persist the smallest amount of bridge state that keeps routing and recovery possible.
- Do not duplicate native thread, turn, approval, model, or reasoning state locally unless there is a clear temporary compatibility need.
- If local state can drift from native state, treat that as a design smell and reduce it.
- Rehydration and reconciliation should restore trust in native state, not preserve stale bridge assumptions.

## Development Rules

- Build thin translations over native capabilities instead of parallel local workflows.
- Add bridge abstractions only when native Codex cannot express the need directly.
- Treat visibility as a presentation concern. Hidden output may still need to be ingested, routed, or accounted for.
- Fail unsupported or inconsistent states explicitly. Do not silently hang, guess, or mask mismatches between bridge and native state.
- When adding a feature, first ask which layer should own it. If the answer is unclear, the design is probably too blurry.

## Transport And Observability

- The socket read path must stay fast.
- Logging, tracing, and diagnostics must not block transport or create backpressure.
- High-volume native notifications should be reduced at the source when they are not required for the chosen UX.
- Disconnect and reconnect paths must aggressively reconcile active turn state before trusting cached local status.
- Recovery paths should leave the system in a smaller, more truthful state, not a larger speculative one.

## What To Avoid

- Do not regrow a second local agent system inside the bridge.
- Do not let bridge-owned configuration override native authority on execution semantics.
- Do not encode product behavior rules as hidden architectural coupling.
- Do not keep compatibility code after its owning constraint is gone.
- Do not optimize for cleverness when a simpler, more inspectable design is available.

## When In Doubt

- If a change introduces a new local source of truth where native Codex already has one, it is probably the wrong change.
- If a proposal makes the bridge thicker without clear user or operational benefit, reject or simplify it.
- If a failure mode is hard to explain, reproduce, or observe, add observability before adding more behavior.
