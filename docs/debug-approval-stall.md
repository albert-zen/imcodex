# Debug Finding: Approval Stall

## Scenario

Executed with:

```powershell
python -m imcodex debug --lab-root D:\desktop\imcodex-debug-lab scenario approval-stall --port 8017
```

Artifacts:

- `run_id`: `debug-20260419-014658`
- `instance_id`: `20260418-174658-p54728`
- `run_dir`: `D:\desktop\imcodex-debug-lab\run\debug-20260419-014658`

## Scenario Setup

The harness injected:

- a conversation binding to `thread_id = thr-debug`
- an active turn: `turn-debug`
- a pending approval request:
  - `request_id = native-request-abcdef`
  - `kind = approval`
  - `request_method = item/commandExecution/requestApproval`

Then it sent:

```text
/approve native-request-abcdef
```

## Observed Result (Original Reproduction)

Before approval:

- `pending_requests` contained `native-request-abcdef`
- `active_turn.turn_id = turn-debug`
- `active_turn.status = inProgress`

Bridge response was:

- `[System] Request native-request-abcdef is no longer pending.`

After approval attempt:

- `pending_requests = []`
- `active_turn.turn_id = turn-debug`
- `active_turn.status = inProgress`
- app-server runtime reported:
  - `pending_server_request_ids = []`

## Conclusion

This reproduces a bridge/native state mismatch:

- the bridge removes the stored pending route
- but the active turn remains in progress
- the request is treated as gone from the bridge point of view
- there is no evidence that the turn was actually advanced or completed

This strongly supports the current hypothesis:

1. bridge-local request state and app-server request state diverge
2. `unknown pending request` is treated as "truly expired"
3. the route is removed from the store
4. the native turn can remain stuck

## Why This Matters

This matches the real user-facing symptom:

- approving a ticket can yield "no longer pending"
- the turn appears stalled afterward
- `/stop` may be required to recover

## Current Status After Mitigation

The bridge no longer blindly treats `unknown pending request` as a confirmed expiry when the same turn is still active locally.

Current behavior:

- if the request route is stale **and** the same turn is still active
- the bridge first tries to interrupt that active turn automatically
- if interruption succeeds, the user gets a recovery status instead of a silent stall
- if interruption cannot be completed automatically, the user gets an explicit out-of-sync message and `/stop` guidance

This means the worst behavior has been narrowed from:

- drop route
- keep turn stuck
- force the user to infer what happened

to:

- detect desync
- try automatic recovery
- fail loudly with specific guidance if recovery is not possible

## Follow-Up Work

Recommended next steps:

1. Record request-route lifecycle events:
   - route created
   - route replied
   - route evicted locally
2. Add a reconciliation path between bridge store state and app-server pending request state.
3. Re-run this scenario against a naturally generated approval request so the active turn id is guaranteed to match a real native turn.
4. Reduce or eliminate cases where `/stop` is still needed after automatic recovery fails.
