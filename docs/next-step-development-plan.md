# IMCodex Next-Step Development Plan

This document turns the current state of `imcodex` into a practical next-step
plan. It is intentionally biased toward execution rather than ideation.

It should be read together with:

- `README.md` for current setup and supported entry points
- `docs/issue-notes.md` for the main classes of problems we already observed
- `todo.md` for product-direction notes around `cwd` semantics and session portability

## 1. Where We Are

The bridge already has the right high-level shape:

- inbound channel messages become `InboundMessage`
- `BridgeService` routes slash commands versus normal text
- `CodexBackend` manages thread and turn lifecycle against `codex app-server`
- `MessageProjector` converts asynchronous Codex notifications into IM-visible messages
- outbound delivery is split between direct channel sinks and optional webhook delivery

This is enough to prove the model, but not yet enough to make the system feel
predictable as a production-grade IM coding assistant.

The biggest gap is not raw functionality. It is product and protocol coherence:

1. users do not yet have one clear mental model for "current working context"
2. sync and async message delivery are not yet defined as one explicit contract
3. session continuity across restarts and across Codex surfaces is not yet first-class
4. Windows runtime and long-lived local process behavior still need stronger operational guardrails

## 2. Planning Principles

The next phase should follow these principles:

- Prefer one user-facing concept: `cwd`
- Keep `project_id` as an internal persistence detail
- Treat inbound webhook response and outbound async callbacks as one protocol, not two unrelated paths
- Preserve native Codex identity such as `threadId` whenever possible
- Optimize for debuggability on Windows, because local iteration failures are often environment failures first
- Add observability before adding more surface area

These principles come directly from the issues already captured in
`docs/issue-notes.md`.

## 3. Recommended Roadmap

## Phase 1: Make The Messaging Contract Explicit

### Goal

Remove ambiguity about when the IM platform should expect an immediate response
 versus an asynchronous follow-up.

### Why now

This is the shortest path to fixing the class of problems where the bridge is
still working but the IM platform appears silent or stalled.

This phase is also the best leverage point for your own IM platform work,
because it defines what your frontend or message transport should display.

### Scope

- Define the canonical lifecycle of one user request:
  - inbound accepted
  - optional progress updates
  - optional approval or question request
  - terminal result
- Define which messages are only returned synchronously from
  `/api/channels/webhook/inbound`
- Define which messages are only delivered through `IMCODEX_OUTBOUND_URL` or a
  channel sink
- Document message ordering expectations and acceptable races
- Make message types stable and intentional from a product point of view

### Expected outputs

- A short protocol document for sync and async bridge messages
- A single message-state diagram in docs
- A clear recommendation for IM clients:
  show sync `accepted` immediately, then merge async `turn_progress`,
  `approval_request`, `question_request`, and `turn_result`

### Implementation notes

- Do not rely on the initial `turn/start` result alone
- Keep trusting later notifications such as `turn/started`,
  `item/completed`, and `turn/completed`
- Be careful not to over-stream token deltas into chat

### Risks

- Over-specifying message order may make the system brittle
- Under-specifying terminal-state rules will recreate the same confusion later

### Acceptance criteria

- A platform integrator can answer:
  "Which response arrives immediately, and which response arrives later?"
- A single turn can be represented cleanly in the IM client without guessing
- Progress and final result can both be shown without duplicate terminal output

## Phase 2: Converge User Semantics On `cwd`

### Goal

Reduce user-facing cognitive load by treating working directory as the primary
concept and hiding internal project identity.

### Why now

Current command wording still teaches both `project` and `cwd`, even though
they are nearly the same from the user's perspective. This is already captured
in `todo.md`, and it is the right product move.

### Scope

- Update command wording and help text to prefer `cwd`
- Rework `/status` output so it is human-readable in chat
- Rework `/projects` and `/project use` into a more natural `cwd`-centric flow
- Keep internal deduplication keyed by normalized path
- Decide whether the old `project` commands remain as compatibility aliases

### Expected outputs

- User-facing command spec centered on `cwd`
- Revised text for:
  - `/status`
  - `/cwd <path>`
  - list/select flows
- Backward-compatible command aliases where helpful

### Implementation notes

- This should mostly be a UX and naming refactor, not a store rewrite
- Keep `project_id` in state and tests, but stop teaching it by default

### Risks

- A partial rename can create a worse hybrid vocabulary
- Removing project identifiers entirely may make support and debugging harder

### Acceptance criteria

- A new user can understand how to select a working context without learning
  two concepts
- Normal users never need to see raw `project_id`
- Existing persisted state still works after the wording change

## Phase 3: Make Session Continuity A First-Class Feature

### Goal

Let a session started in IM be resumed reliably after restart and, where
possible, across Codex surfaces.

### Why now

This is the main feature that would make the bridge feel like part of the
Codex ecosystem rather than a one-off wrapper.

### Scope

- Persist and expose canonical thread identity more deliberately
- Investigate `thread/resume` support and expected resume inputs
- Define what "resume" means in this product:
  - same IM conversation after bridge restart
  - attach to a thread started elsewhere
  - continue a thread from IM in CLI or desktop
- Add explicit commands or attach flows if needed

### Expected outputs

- A technical design note for session portability
- A tested resume workflow across at least one restart path
- A decision on whether session path also needs to be surfaced in addition to
  `threadId`

### Implementation notes

- Prefer native Codex identifiers over bridge-invented abstractions
- Treat stale or poisoned threads as an expected failure mode, not an edge case

### Risks

- Codex-native resume semantics may differ from assumptions made by the bridge
- Cross-surface portability may require metadata that the current bridge does
  not persist yet

### Acceptance criteria

- After restarting `imcodex`, an existing IM conversation can continue without
  silently losing its thread context
- The bridge can distinguish:
  valid resumable thread, stale thread, and missing thread
- Resume failure paths are visible to the user instead of looking like a hang

## Phase 4: Strengthen Operational Guardrails On Windows

### Goal

Make local runtime behavior predictable during iterative development and demo use.

### Why now

The issue notes show that Windows-specific process, port, and executable
ambiguity repeatedly caused false diagnoses.

### Scope

- Add a lightweight diagnostics command or startup log summary for:
  - codex executable path
  - app-server host and port
  - IM bridge port
  - process id
  - data directory
- Add a documented restart checklist for local development
- Decide whether to harden app-server spawn behavior further on Windows
- Improve detection and reporting when an old process is still bound to a port

### Expected outputs

- Better startup logging
- A local-ops troubleshooting section in docs
- Possibly a `/doctor` or equivalent diagnostic command

### Implementation notes

- This is as much product work as infra work
- The point is to shorten debugging loops, not to build a full admin panel

### Risks

- Too much operational output in normal chat can be noisy
- Too little runtime verification will keep wasting time during local iteration

### Acceptance criteria

- A developer can quickly tell which process is serving traffic
- Port confusion and stale-process confusion can be identified within minutes
- The most common Windows setup failures have direct diagnostics

## Phase 5: Add End-To-End Confidence For The Real Integration Paths

### Goal

Raise confidence in the paths that matter most: webhook integration, QQ async
delivery, approvals, and long-running turns.

### Why now

The repo already has a healthy amount of unit coverage. The next quality jump
comes from more opinionated integration coverage around the actual message flow.

### Scope

- Add end-to-end tests for:
  - sync webhook response plus async outbound follow-up
  - approval request and approval response loop
  - question request and answer loop
  - interrupted turn
  - stale thread recovery
  - final answer before `turn/completed`
- Validate the IM-facing message contract, not just internal method calls

### Expected outputs

- A small set of scenario-driven integration tests
- Fixture data that mirrors the message types the IM platform really consumes

### Implementation notes

- These tests should focus on product-visible behavior
- Prefer a few high-value scenarios over a large matrix of synthetic cases

### Risks

- Too much mocking can create false confidence
- Overly detailed assertions can freeze the protocol before it is ready

### Acceptance criteria

- The most failure-prone flows from `docs/issue-notes.md` are covered by tests
- A future change to projector or bridge messaging breaks tests when user-visible
  behavior regresses

## 4. Suggested Execution Order

Recommended order for the next development cycle:

1. Phase 1: messaging contract
2. Phase 2: `cwd`-first user semantics
3. Phase 5: end-to-end coverage for the new contract
4. Phase 3: session continuity and resume
5. Phase 4: Windows operational hardening

Why this order:

- Phase 1 unblocks your IM platform integration directly
- Phase 2 reduces product ambiguity before more features are added
- Phase 5 stabilizes the contract before deeper session work
- Phase 3 is strategically important, but it should build on a cleaner message
  model and vocabulary
- Phase 4 should continue in parallel where convenient, but it should not delay
  the product-contract work

## 5. Concrete Next Sprint Recommendation

If we compress this into one practical sprint, the most valuable slice is:

1. Write the sync/async messaging contract and message-state diagram
2. Update message and command wording to be `cwd`-first
3. Add one end-to-end test proving:
   webhook ack returns immediately, then async final result is delivered later
4. Add one end-to-end test covering approval or question flow
5. Add startup diagnostics that reveal active ports and executable paths

This sprint would not solve everything, but it would turn the current bridge
from "technically working" into "integration-ready and explainable".

## 6. Open Questions

These should be answered before deeper implementation begins:

- Should the IM platform itself persist and render turn state, or should the
  bridge remain the single source of truth for status transitions?
- Do you want the IM client to display progress updates as separate messages or
  to patch an existing message in place when the channel supports it?
- Is cross-surface continuation a hard requirement for the next milestone, or a
  second-phase capability?
- Should auto-approve remain primarily an environment-level switch, or do you
  want per-conversation policy later?

## 7. Definition Of Success

The next milestone is successful if:

- a user can pick a working directory with minimal explanation
- the IM platform always knows whether to expect sync or async follow-up
- long-running turns feel alive rather than silent
- approvals and question prompts survive the round trip reliably
- restart and stale-thread failure modes are visible and recoverable
- the bridge feels like a stable Codex surface, not a fragile adapter
