# IMCodex Next-Step Development Plan

This document turns the current state of `imcodex` into a practical roadmap.
It is intentionally biased toward sequencing and dependencies rather than
brainstorming.

Read this together with:

- `README.md` for current setup and entry points
- `todo.md` for the concise roadmap view
- `docs/message-contract.md` for the current sync/async bridge contract
- `docs/issue-notes.md` for the main classes of problems we already hit
- `docs/deployment.md` for current deployment and diagnostics workflow

## 1. Where We Are Now

The bridge already has a solid baseline:

- three-layer structure:
  - `imcodex.channels`
  - `imcodex.bridge`
  - `imcodex.appserver`
- generic webhook entry plus QQ channel support
- native `codex app-server` integration
- slash command routing and conversation binding
- approval and question round trips
- auto-approve support
- documented sync/async message contract
- deployment packaging and diagnostics scripts

That means the next milestone is no longer "make it work at all".
The next milestone is:

- make it easier to understand in chat
- make sessions feel native and portable
- make runtime behavior easier to trust on Windows

## 2. What Is Already Done

The following should no longer be treated as the primary "next step":

### 2.1 Messaging contract baseline

- `docs/message-contract.md` exists
- sync webhook acknowledgement versus async follow-up is documented
- the bridge already distinguishes:
  - immediate command results
  - immediate accepted responses
  - async progress
  - approval or question requests
  - terminal turn results

### 2.2 Deployment packaging baseline

- `.env.example` exists
- `scripts/start.ps1` exists
- `scripts/doctor.ps1` exists
- `docs/deployment.md` exists
- host and port configuration are environment-driven

### 2.3 Structural simplification baseline

- channel-specific logic is separated from bridge logic
- app-server protocol logic is separated from channel logic
- composition and runtime are thin wiring layers

## 3. Current Product Gaps

The biggest remaining gaps are now product and lifecycle gaps rather than
missing infrastructure.

### 3.1 Working-context semantics are still more complex than they need to be

Users should only need to understand one concept for where Codex is working.
Today, the system still carries too much historical `project` vocabulary.

### 3.2 Thread management is still too machine-oriented

Canonical thread ids are necessary, but they are not good primary UX.
Thread lists and status output still need more human-readable labels and better
attach/resume ergonomics.

### 3.3 Tool activity is still under-explained in chat

The system already pushes progress and final output, but users still do not get
the best possible visibility into what Codex is actively doing during a longer
task.

### 3.4 Session continuity across Codex surfaces is only partially realized

The bridge now has the right building blocks, but portability still needs to be
made explicit and tested as a first-class workflow.

### 3.5 Windows runtime trust still depends too much on manual inspection

Diagnostics are better than before, but long-running service behavior and
restart trust still need more productized handling.

## 4. Planning Principles

The next phase should follow these principles:

- Prefer one user-facing concept: `cwd`
- Preserve native Codex identity whenever possible
- Keep channel identity, thread identity, turn identity, and request identity separate
- Show enough progress to keep long-running turns alive without flooding the chat
- Improve operability in ways that shorten debugging loops on Windows
- Favor a few high-value end-to-end scenarios over large synthetic test matrices

## 5. Recommended Roadmap

## Phase 1: Make The IM Experience Coherent

### Goal

Reduce confusion in day-to-day chat usage.

### Scope

- converge user-facing semantics on `cwd`
- improve thread readability
- reduce noisy routing and processing chatter
- make tool activity more legible in IM

### Why this comes first

This is the highest-value user-facing work remaining.
The bridge already works, but it still feels more like a capable prototype than
a polished conversational coding surface.

### Acceptance criteria

- `/status` is readable in chat and `cwd`-first
- thread lists are understandable without raw ids
- routine chat no longer emits low-value routing chatter
- users can tell what Codex is doing during longer tasks

## Phase 2: Make Session Portability First-Class

### Goal

Let IM sessions participate naturally in the broader Codex ecosystem.

### Scope

- preserve and expose canonical thread identity
- strengthen `thread/resume` and attach workflows
- define what portability means across:
  - IM bridge
  - Codex CLI
  - Codex Desktop
- test restart and resume flows explicitly

### Why this comes second

It is strategically important, but the user-facing vocabulary and thread UX
should be cleaned up first so that attach/resume flows land on a clearer model.

### Acceptance criteria

- restart does not silently lose conversation continuity
- an existing native thread can be attached in the bridge
- the bridge can explain resume success versus stale-thread fallback

## Phase 3: Strengthen Runtime Operability

### Goal

Make the service easier to trust and easier to keep running.

### Scope

- stronger startup summaries and runtime diagnostics
- clearer process and port ownership visibility
- improved long-running Windows service behavior
- better logging and troubleshooting guidance

### Why this follows portability

Operability matters a lot, but the current baseline is already good enough to
support the next round of product work. This phase should tighten the service,
not block product iteration.

### Acceptance criteria

- it is easy to identify the live bridge process and app-server process
- restart failures can be diagnosed quickly
- logs and diagnostics are predictable enough for another machine or operator

## Phase 4: Add More Real-Flow Confidence

### Goal

Protect the behaviors users actually feel.

### Scope

- add more scenario-driven end-to-end tests around:
  - progress delivery
  - approval and question loops
  - interrupted turns
  - stale-thread recovery
  - restart continuity
  - cross-surface attach and resume

### Why this is last

Coverage is already decent. The next confidence jump should lock down the
behaviors we decide on in Phases 1 through 3, rather than freezing today’s UX
too early.

### Acceptance criteria

- the most failure-prone user-visible flows are covered by tests
- projector or bridge regressions break tests in a meaningful way

## 6. Dependency Order

Recommended order for the next development cycle:

1. Phase 1: IM experience coherence
2. Phase 2: session portability
3. Phase 3: runtime operability
4. Phase 4: real-flow end-to-end confidence

Why this order:

- Phase 1 improves the product immediately
- Phase 2 builds on clearer `cwd` and thread semantics
- Phase 3 should harden the now-clearer product surface
- Phase 4 should then lock down the chosen behavior

## 7. Concrete Next Sprint Recommendation

If we compress this into one realistic sprint, the best slice is:

1. make `/status`, selection flows, and thread lists fully `cwd`-first
2. improve thread display labels and reduce low-value chat noise
3. expose more meaningful tool activity in IM without raw-token spam
4. add one explicit attach/resume workflow and one restart-continuity scenario test

This sprint would not solve everything, but it would make the bridge feel much
closer to a native Codex surface rather than an integration layer with rough
edges.

## 8. Definition Of A Good Next Milestone

The next milestone is successful if:

- users can understand the current working context without learning internal vocabulary
- thread management feels readable and practical in chat
- long-running turns feel alive rather than silent
- a session can survive restart and begin to move across Codex surfaces
- operators can trust what process is actually live on Windows
