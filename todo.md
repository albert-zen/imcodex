# TODO

## Product semantics

- Unify `project`, `cwd`, and `current folder` into one user-facing concept.
  - User-facing commands and status output should prefer `cwd` terminology.
  - Keep any internal `project_id` only as an implementation detail for persistence and deduplication.
  - Update `/status`, selection flows, and future command wording to avoid teaching two concepts for the same thing.

## Message visibility

- Surface tool calls in the IM experience instead of only showing high-level progress and final output.
  - Users should be able to tell when Codex is searching, reading files, running commands, or waiting on an approval.
  - Decide whether tool calls appear as separate chat messages, structured status events, or expandable details in the client.
  - The sync webhook response and async outbound pipeline should both preserve enough event detail for the client to render tool activity.
  - Avoid raw token spam or overly verbose low-level logs; expose meaningful tool activity rather than every protocol event.

## Thread readability

- Improve thread naming so threads are readable in IM and management commands.
  - Prefer a short topic summary when available.
  - Otherwise fall back to a clipped version of the first user message.
  - Avoid exposing raw UUID-like thread ids as the primary label in user-facing messages.
  - Keep the canonical thread id available for switching/resume, but separate it from the display name.

## Message noise

- Simplify per-message processing notices in the IM experience.
  - Reduce or remove repetitive messages like "accepted for thread ..." and "processing your request" when they do not add real value.
  - Move low-value routing details, including which thread accepted the message, into logs or debug-oriented surfaces instead of the main chat flow.
  - Keep only the minimum visible feedback needed so users know the request was received and work has started.
  - Re-evaluate which status events belong in chat versus logs, especially now that intermediate progress messages can be pushed during execution.

## Session portability

- Make a session for one working directory portable across Codex clients.
  - Goal: a session started from this IM bridge can be resumed from other Codex surfaces, including Codex CLI and Codex Desktop, and vice versa.
  - Prefer native Codex session/thread primitives instead of bridge-only state.
  - Persist and expose the canonical thread identity needed for `thread/resume`.
  - Evaluate whether we also need to surface the persisted session path in addition to `threadId`.
  - Verify practical round-trip flows:
    - start in IM bridge -> continue in Codex CLI
    - start in Codex CLI/Desktop -> attach in IM bridge
    - resume after restart without losing the same thread history
