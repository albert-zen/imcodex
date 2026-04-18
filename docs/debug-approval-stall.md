# Debug Finding: Approval Stall

## Summary

The original user-facing pain was:

- Codex asked for approval
- IMCodex lost the ability to reply to that approval after a reset
- the turn stayed stuck
- the user had to `/stop` before they could continue

Using the debug harness, we now reproduced the issue on a real end-to-end path and closed the main failure mode.

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
  - `transport_request_id = 0`
  - `connection_epoch = 1`
- the real turn was:
  - `status = inProgress`

After reset, before the fix:

- the approval projection was evicted
- `/approve` returned:
  - `[System] Unknown approval request.`
- but the active turn could remain `inProgress`
- sometimes even `/stop` could fail because the new stdio app-server no longer knew the old turn

That meant the stale approval itself was no longer visible, but the turn cleanup path was still incomplete.

## Root Cause

The main remaining problem was not the approval projection itself. It was turn cleanup after reset.

When the stdio app-server connection resets:

1. IMCodex invalidates approval projections for the old `connection_epoch`
2. IMCodex tries to interrupt the affected turn
3. the reset often reconnects to a **new** spawned stdio app-server
4. that new app-server may return errors like:
   - `unknown thread`
   - `no rollout found for thread id ...`

Previously, IMCodex did **not** treat those errors as stale-turn cleanup signals.

So the result was:

- approval projection gone
- active turn still present locally
- conversation looked stuck until `/stop`

## Fix

The backend now treats stale thread errors during `turn/interrupt` the same way it already treated stale turn errors:

- `unknown thread`
- `no rollout found for thread id ...`

These are now interpreted as:

- the old turn is no longer interruptible remotely
- local active-turn state should be cleared
- local pending requests for that turn should be removed

This means reset cleanup can complete even when reconnecting to a fresh stdio app-server instance.

## Verified Result

After the fix, the same live scenario now behaves like this:

After reset:

- stale approval projection is removed
- `/approve` returns:
  - `[System] Unknown approval request.`
- `active_turn = null`
- `pending_requests = []`

Then a normal follow-up message:

- does **not** require `/stop`
- starts a new turn normally
- proves the conversation is no longer wedged

That is the key behavioral change:

- reset no longer leaves the conversation trapped behind a stale paused turn

## Related Observability Fix

The same live scenario also exposed a logging/health inconsistency:

- reset handlers could reconnect to a new app-server
- but `_reset_connection()` still marked health as `disconnected` at the end

That is now fixed as well, so harness output no longer reports a false disconnected state after successful recovery.

## Remaining Semantics

The current behavior after reset is intentionally conservative:

- the stale approval request is treated as no longer actionable
- `/approve` does not attempt to resurrect it
- the user can continue with a new message immediately

For IM, this is preferable to leaving a hidden stale turn that still requires `/stop`.

## Conclusion

The original approval-stall case is now handled end to end:

- a real approval request can be produced
- a reset can invalidate the request
- IMCodex no longer leaves the turn stuck afterward
- a new message can continue the conversation without manual `/stop`

This does **not** make the stale approval itself recoverable after reset, but it does eliminate the stuck-turn failure mode that made the flow unusable.
