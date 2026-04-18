# Restart Executor

`imcodex` bridge restart is now expected to happen through an external executor, not by asking the bridge process to stop itself and somehow finish the rest of the restart afterward.

## Command

```powershell
python -m imcodex ops restart --launch-snapshot <path> --timeout 30
```

The launch snapshot is written into the observability run archive:

- `current/launch.json`
- `<run>/launch.json`

## What it does

The restart executor:

1. reads the launch snapshot
2. stops the current bridge PID
3. starts a fresh bridge process with the same launch environment
4. waits for the new bridge port to become healthy

It returns a compact result:

- new PID
- port
- health summary

## Why this exists

In the old topology, "restart the bridge" often meant:

- the current bridge turn asked to stop the bridge
- the bridge stopped
- no process remained to execute the follow-up start

That produced the observed restart gap:

- bridge stopped
- port disappeared
- no new process appeared

The executor fixes that by moving restart ownership out of the bridge.

## Relationship to dedicated core

The executor is most useful when paired with a dedicated Codex core:

- bridge restarts
- core keeps running
- new bridge reconnects to the same websocket app-server

Without a dedicated core, restart is still possible, but more native state can be lost if the bridge was also responsible for the private `spawned-stdio` app-server.

## Current limitation

Even with a dedicated core, the currently running `codex.exe` does not reliably replay a pending approval to a brand-new websocket client after disconnect.

So a bridge restart during a paused approval turn currently has these properties:

- the core stays alive
- the bridge reconnects successfully
- the old approval may no longer be actionable
- the bridge must still be prepared to clean up the stale paused turn

This is still better than the old self-restart failure mode, because restart itself completes and the bridge comes back.
