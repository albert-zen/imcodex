# Debug Finding: Approval Stall

## Summary

The original user-facing pain was:

- Codex asked for approval
- IMCodex lost the ability to reply to that approval after a reset
- the turn stayed stuck
- the user had to `/stop` before they could continue

Using the debug harness, we reproduced the issue on a real end-to-end path and then kept digging until we could separate three different behaviors:

1. old bridge-local approval mapping drift
2. native approval replay on the same live connection
3. native approval replay after a real websocket disconnect and reconnect

## Live Reproduction

Executed with:

```powershell
python -m imcodex debug scenario approval-live --port 8041
```

The scenario does all of the following against an isolated instance:

1. `/cwd <debug-cwd>`
2. `/new`
3. send a normal-language prompt that asks Codex to run `Get-Date`
4. wait for a real native approval request
5. force a client reset
6. send `/approve`
7. send a normal follow-up message

## What We Observed

Before reset:

- a real native approval request appeared
- the request was projected into the conversation as:
  - `request_method = item/commandExecution/requestApproval`
  - `transport_request_id = <n>`
  - `connection_epoch = 1`
- the real turn was `inProgress`

After reset, before the fix:

- the approval projection was evicted
- `/approve` returned:
  - `[System] Unknown approval request.`
- but the active turn could remain `inProgress`
- sometimes even `/stop` could fail because the new stdio app-server no longer knew the old turn

That meant the stale approval itself was no longer visible, but the turn cleanup path was still incomplete.

## Root Cause

The first root cause was bridge-local:

- the bridge used to depend on a client-side pending-request map
- reset could clear that local map while native still thought a request existed
- IM commands could no longer target the native request reliably

That problem was fixed by moving approval routing to native request projections.

The second root cause is native/runtime-specific:

- on the actual `codex.exe` binary we are running, a pending approval **does** replay on `thread/resume` within the same connection
- but after a real websocket disconnect, a brand-new client reconnecting to the same dedicated core does **not** receive that approval back via `thread/resume`

So even in the new dedicated-core architecture, the bridge cannot assume:

- "disconnect + reconnect + thread/resume" will always replay the approval

That assumption is true in upstream code/tests, but it is not currently true in the runtime binary we actually probed.

## Fix

The bridge now does two things:

1. it keeps approval routing thin and native-first
2. it still performs fallback turn cleanup when approval replay is lost after reset

The backend treats stale thread errors during `turn/interrupt` the same way it already treated stale turn errors:

- `unknown thread`
- `no rollout found for thread id ...`

These are now interpreted as:

- the old turn is no longer interruptible remotely
- local active-turn state should be cleared
- local pending requests for that turn should be removed

This means reset cleanup can complete even when reconnecting to:

- a fresh stdio app-server instance
- or a dedicated websocket core that no longer replays the approval to the new bridge connection

## Verified Result

After the fix, the same live scenario behaves like this:

After reset:

- stale approval projection is removed
- `/approve` may return:
  - `[System] Unknown approval request.`
- the conversation is no longer permanently wedged
- the next reconnect / follow-up message can clear the stale turn without requiring manual `/stop`

Then a normal follow-up message:

- does **not** require `/stop`
- starts a new turn normally
- proves the conversation is no longer wedged

That is the key behavioral change:

- reset no longer leaves the conversation trapped behind a stale paused turn

## Native Probe Finding

Using a direct client probe against the dedicated websocket core, we verified:

- same-connection `thread/resume` can replay a pending approval
- disconnect + brand-new client + `thread/resume` does **not** replay it in the currently running native binary

That means the bridge must keep a fallback cleanup path even after the dedicated-core refactor.

## Related Observability Fix

The same live scenario also exposed a logging/health inconsistency:

- reset handlers could reconnect to a new app-server
- but `_reset_connection()` still marked health as `disconnected` at the end

That is now fixed as well, so harness output no longer reports a false disconnected state after successful recovery.

## Remaining Semantics

The current behavior after reset is intentionally conservative:

- the stale approval request is treated as no longer actionable
- `/approve` does not try to fake-resurrect it
- the user can continue with a new message immediately

For IM, this is preferable to leaving a hidden stale turn that still requires `/stop`.

## Conclusion

The original approval-stall case is now handled end to end:

- a real approval request can be produced
- a reset can invalidate the request
- IMCodex no longer leaves the turn stuck afterward
- a new message can continue the conversation without manual `/stop`

This does **not** make the stale approval itself recoverable after reset in every reconnect path, but it does eliminate the stuck-turn failure mode that made the flow unusable.
