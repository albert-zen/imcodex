# Debug Finding: Approval Stall

## Scenario

Executed with:

```powershell
python -m imcodex debug --lab-root D:\desktop\imcodex-debug-lab scenario approval-stall --port 8022
```

Artifacts:

- `run_id`: `debug-20260419-020855-007866`
- `instance_id`: `20260418-180855-p52000`
- `run_dir`: `D:\desktop\imcodex-debug-lab\run\debug-20260419-020855-007866`

## Scenario Setup

The harness injects four pieces of state:

- a conversation binding to `thread_id = thr-debug`
- an active turn: `turn-debug`
- a store-side pending approval request:
  - `request_id = native-request-abcdef`
  - `kind = approval`
  - `request_method = item/commandExecution/requestApproval`
- a client-side pending request entry:
  - `_pending_server_requests["native-request-abcdef"] = {"id": 99}`

Then the harness forces a client reset before sending:

```text
/approve native-request-abcdef
```

This gives a controlled reproduction of the exact condition we care about:

- `ConversationStore` still believes the request is pending
- `AppServerClient` has forgotten it after reset

## Observed Result

Before forced reset:

- `pending_requests` contained `native-request-abcdef`
- `active_turn.turn_id = turn-debug`
- `active_turn.status = inProgress`
- runtime reported:
  - `pending_server_request_ids = ["native-request-abcdef"]`

After forced client reset:

- runtime reported:
  - `connection_mode = disconnected`
  - `pending_server_request_ids = []`
- store still reported:
  - `pending_requests` contained `native-request-abcdef`
  - `active_turn.turn_id = turn-debug`

Bridge response was:

- `[System] Request native-request-abcdef is out of sync with Codex and the active turn could not be stopped automatically. Try /stop.`

After approval attempt:

- `pending_requests` still contained `native-request-abcdef`
- `active_turn.turn_id = turn-debug`
- `active_turn.status = inProgress`
- runtime later reconnected, but there was still no evidence that the original turn had advanced or completed

## Conclusion

This reproduces the specific bridge/native mismatch we care about:

- `ConversationStore` still holds the pending route and active turn
- `AppServerClient._pending_server_requests` is wiped on `_reset_connection()`
- `/approve` then goes through the desync recovery path because the client no longer recognizes the request id
- the bridge can only auto-recover if it can successfully interrupt that active turn
- when that interrupt path does not complete, the system falls back to an explicit `/stop` recommendation

This narrows the root cause substantially:

1. bridge-local request state and app-server client request state diverge across connection reset
2. the store has no reconciliation hook when `_reset_connection()` clears client pending requests
3. `serverRequest/resolved` is the canonical cleanup signal in the native protocol, but reset can destroy client memory before that signal is observed locally
4. once that happens, `/approve` is no longer a normal reply path; it is a desync recovery path

## Real Thread Confirmation

The same pattern was reproduced on a real native thread:

1. start an isolated debug run
2. use `/cwd ...` and `/new` to create a real thread
3. inject:
   - `active_turn = turn-real`
   - store pending route `native-request-real`
   - client pending entry `native-request-real`
4. force client reset
5. run `/approve native-request-real`

Observed result:

- the response was again:
  - `[System] Request native-request-real is out of sync with Codex and the active turn could not be stopped automatically. Try /stop.`
- the conversation still showed:
  - `active_turn = turn-real`
  - `pending_requests` still present
- event log showed:
  - `appserver.connection.closed`
  - followed by reconnect events

This means the harness result is not just synthetic; the same failure mode survives when the thread itself is real and comes from native Codex.

## Why This Matters

This matches the real user-facing symptom:

- a ticket can still look actionable in IM
- but by the time the user approves, the bridge and native client may already disagree about whether that request still exists
- the turn can remain stalled afterward
- `/stop` may still be required to recover

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

That mitigation is useful, but the harness now shows it is not the full fix.

## Follow-Up Work

Recommended next steps:

1. Add explicit reconciliation between `AppServerClient._reset_connection()` and bridge-side pending routes.
2. Record request lifecycle events around:
   - route created
   - client pending created
   - client reset
   - route reconciled or evicted
3. Distinguish "locally forgotten after reset" from "truly resolved by native Codex".
4. Reduce or eliminate cases where `/stop` is still needed after automatic recovery fails.
