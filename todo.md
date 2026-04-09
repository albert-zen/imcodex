# IMCodex Roadmap

This file is the working product roadmap for `imcodex`.

It is intentionally short and execution-oriented:

- `Now` means the next milestone we should actively build.
- `Next` means the following milestone once `Now` is stable.
- `Later` means important, but not worth delaying the current milestone.

Read this together with:

- `README.md` for the current supported setup
- `docs/message-contract.md` for the current sync/async bridge contract
- `docs/issue-notes.md` for the main problem classes we have already hit
- `docs/next-step-development-plan.md` for the more detailed development plan

## Done Foundations

These are already in place and should now be treated as baseline, not future work:

- Core three-layer structure:
  - `imcodex.channels`
  - `imcodex.bridge`
  - `imcodex.appserver`
- Generic webhook bridge flow and QQ channel support
- Native `codex app-server` integration
- Basic slash commands and conversation binding
- Approval and question round trips
- Auto-approve environment switches
- Sync/async message contract baseline
- Deployment packaging:
  - `.env.example`
  - `scripts/start.ps1`
  - `scripts/doctor.ps1`
  - `docs/deployment.md`

## Now: Make The IM Experience Coherent

This is the highest-value next milestone because it directly improves daily use.

### 1. Unify User Semantics On `cwd`

- Treat `cwd`, `current folder`, and working directory as one user-facing concept.
- Stop teaching `project` as a separate concept in normal chat flows.
- Keep internal `project_id` only as a persistence and deduplication detail.
- Update `/status`, selection flows, and command wording to be `cwd`-first.

### 2. Make Threads Human-Readable

- Improve thread labels so users can understand thread lists without reading raw ids.
- Prefer a short summary or topic label when available.
- Otherwise fall back to a clipped version of the first meaningful user message.
- Keep canonical `threadId` available for attach/resume, but never as the primary label.

### 3. Reduce Message Noise

- Remove or compress low-value messages like repeated accept/process notices.
- Keep only the minimum visible feedback needed to show that work has started.
- Move routing detail such as thread-selection chatter into logs or debug-oriented surfaces.

### 4. Surface Tool Activity Better

- Show meaningful tool activity in IM:
  - searching
  - reading files
  - running commands
  - waiting for approval or input
- Avoid raw token spam and low-level protocol chatter.
- Decide what belongs in chat versus what should remain a client-side detail or debug log.

## Next: Make Sessions Portable Across Codex Surfaces

This is the strategic feature that makes the bridge feel native to the Codex ecosystem.

### 5. Cross-Surface Session Continuity

- Let a session started in IM be resumed from Codex CLI or Codex Desktop when possible.
- Let a thread started elsewhere be attached inside the IM bridge.
- Prefer native Codex thread/session primitives instead of bridge-only abstractions.
- Persist and expose the canonical identifiers needed for `thread/resume`.

### 6. Resume And Attach Flows

- Make restart recovery feel normal, not exceptional.
- Clarify and test:
  - IM bridge restart -> continue the same conversation
  - Codex CLI/Desktop thread -> attach in IM bridge
  - IM bridge thread -> continue outside the bridge
- Decide whether surfacing thread id is enough, or whether session path also matters.

## Later: Make The Service Easier To Operate

This work matters, but should not block the UX and portability milestone.

### 7. Runtime Hardening

- Improve long-running Windows service behavior.
- Add stronger startup and restart diagnostics.
- Make it easy to tell which process owns:
  - the IM bridge port
  - the app-server port
  - the active data directory
- Consider a more explicit service-mode or background-runner story.

### 8. Logging And Health

- Standardize runtime logs and log locations.
- Add a simple health or diagnostics surface beyond ad hoc terminal inspection.
- Keep debugging loops short when the environment, not the code, is broken.

## Later: Raise Product Confidence

### 9. Higher-Value End-To-End Coverage

- Add more scenario-driven end-to-end tests around:
  - long-running turns
  - partial progress delivery
  - approval and question loops
  - restart and stale-thread recovery
  - cross-surface attach and resume

### 10. Stronger Product Contract Docs

- Keep the IM-facing protocol and operational docs aligned with real behavior.
- Make it easy for another developer to understand:
  - what arrives synchronously
  - what arrives asynchronously
  - what state is bridge-owned
  - what state is Codex-native

## Ordering Principles

When choosing work, prefer this order:

1. Reduce user confusion in chat
2. Preserve native Codex identity and continuity
3. Improve operability on Windows
4. Expand confidence with end-to-end coverage

If a change does not improve one of those four things, it is probably not the next best use of time.
