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
python -m imcodex debug scenario approval-stall --port 8017
```

## Built-In Scenarios

### `restart-gap`

Creates an isolated instance, waits for health, stops it, and records whether anything restarts it automatically.

Used to debug:

- bridge stop/start gaps
- lack of auto-restart
- health and event evidence after termination

### `approval-stall`

Creates an isolated instance, injects a bound conversation, active turn, and pending approval route, then sends `/approve <request_id>`.

Used to debug:

- stale pending request routes
- bridge/native request state mismatches
- turns that remain in progress after bridge has dropped the request route

## Notes

- The harness is intentionally external. It observes and drives the target instance; it does not require the target instance to orchestrate itself.
- The harness currently uses the webhook ingress path rather than a new production-only debug transport.
- The built-in scenarios are designed to produce evidence, not to hide failure modes.
