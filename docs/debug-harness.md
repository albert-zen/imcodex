# Debug Harness

`imcodex debug` provides an external observer workflow for starting isolated bridge instances, sending synthetic inbound messages, and inspecting runtime state without going through QQ.

## Why It Exists

The bridge and native Codex runtime can fail in ways that are hard to reproduce manually:

- bridge restart gaps
- stale approval routes
- thread resume edge cases
- request/turn state mismatches

The debug harness gives us a repeatable way to probe those flows with a dedicated test CWD and isolated runtime directories.

## Isolation Model

Every debug run gets its own directories under a separate lab root:

- `cwd/<run_id>/`
- `data/<run_id>/`
- `run/<run_id>/`
- `manifests/<run_id>.json`

The default lab root is:

- `D:\desktop\imcodex-debug-lab`

This keeps test threads separated from normal work threads. The harness writes `.imcodex-debug-session.json` into each debug CWD so native threads are easy to identify later.

Run ids preserve sub-second precision, for example:

- `debug-20260419-020855-007866`

This avoids collisions when multiple isolated runs start within the same second.

## Commands

### Start

```powershell
python -m imcodex debug start --port 8012 --wait --purpose harness-smoke
```

Starts an isolated instance with:

- its own HTTP port
- its own `IMCODEX_DATA_DIR`
- its own observability run directory
- `QQ` disabled by default
- debug inspection API enabled

### Stop

```powershell
python -m imcodex debug stop --run-id debug-20260419-013713
```

Stops a run managed by the harness.

### List Runs

```powershell
python -m imcodex debug runs
```

Shows manifests for known debug runs.

### Send

Send a message into a conversation:

```powershell
python -m imcodex debug send --run-id debug-20260419-013713 --conversation conv-1 --text "/cwd playground"
```

Send to a specific thread by attaching first and then sending the message:

```powershell
python -m imcodex debug send --run-id debug-20260419-013713 --conversation conv-1 --thread 019d... --text "continue"
```

### Inspect

Inspect instance, health, conversation, and runtime state:

```powershell
python -m imcodex debug inspect --run-id debug-20260419-013713 --conversation conv-1 --live
```

Inspect a specific thread:

```powershell
python -m imcodex debug inspect --run-id debug-20260419-013713 --thread 019d...
```

### Events

Tail structured events from a run archive:

```powershell
python -m imcodex debug events --run-id debug-20260419-013713 --tail 50
```

### Scenarios

Run the built-in reproduction scenarios:

```powershell
python -m imcodex debug scenario restart-gap --port 8016
python -m imcodex debug scenario approval-stall --port 8022
python -m imcodex debug scenario approval-live --port 8024
```

## Built-In Scenarios

### `restart-gap`

Creates an isolated instance, waits for health, stops it, and records whether anything restarts it automatically.

Used to debug:

- bridge stop/start gaps
- lack of auto-restart
- health and event evidence after termination

### `approval-stall`

Creates an isolated instance, creates a real thread with `/cwd` and `/new`, then injects a native-shaped approval request through the real projection path before forcing a client reset.

Used to debug:

- request projection behavior across client reset
- bridge/client request state mismatches after reset
- approval routes that become invalid before the user replies

### `approval-live`

Creates an isolated instance, then drives a normal-language prompt that causes native Codex to request approval for a real PowerShell command.

The scenario then:

- waits for the real approval request to appear
- forces a client reset
- sends `/approve`
- verifies that the stale approval is gone
- sends a normal follow-up message and waits for a new turn to start

Used to debug:

- approval behavior on a normal end-to-end path
- reset cleanup for real paused turns
- whether the conversation can continue without `/stop`

### Native reconnect probe

For the dedicated-core architecture, we also used the harness and a direct probe client to verify a subtle but important runtime fact:

- same-connection `thread/resume` can replay a pending approval
- after a real websocket disconnect, a brand-new client reconnecting to the same core does not currently get that approval replayed back in the runtime binary we are using

That finding matters because it means:

- dedicated core still improves lifecycle separation
- but bridge fallback cleanup for lost approvals cannot be deleted completely yet

## Notes

- The harness is intentionally external. It observes and drives the target instance; it does not require the target instance to orchestrate itself.
- The harness currently uses the webhook ingress path rather than a new production-only debug transport.
- The built-in scenarios are designed to produce evidence, not to hide failure modes.
