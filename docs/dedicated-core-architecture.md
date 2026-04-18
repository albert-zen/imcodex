# Dedicated Core Architecture

`imcodex` is moving away from the old "bridge spawns a private stdio app-server and treats that as the main path" model.

The target topology is:

1. a long-lived dedicated Codex core
2. a separate IM bridge process
3. an external restart executor for bridge hot reloads

## Why

The old `spawned-stdio` topology made two classes of bugs much worse:

- bridge restarts could replace the entire native app-server instance, which destroyed native turn and approval state
- self-restart flows stalled at `stop` because the bridge was trying to kill the same process that still needed to execute the `start`

By separating core and bridge:

- native thread and turn state can survive bridge restarts
- bridge restart becomes a normal client reconnect problem
- approval, resume, and interrupt paths can lean harder on native app-server behavior

## Topology

### Dedicated Codex core

Started independently, for example:

```powershell
python -m imcodex core start --port 8765
```

This launches:

```text
codex app-server --listen ws://127.0.0.1:8765
```

The dedicated core is the source of truth for:

- thread state
- turn state
- pending native approval requests
- resume and interrupt behavior

### IM bridge

Started separately and pointed at the dedicated core:

```powershell
$env:IMCODEX_CORE_MODE = "dedicated-ws"
$env:IMCODEX_CORE_URL = "ws://127.0.0.1:8765"
python -m imcodex
```

In this mode the bridge:

- must connect to the dedicated websocket app-server
- will not silently fall back to `spawned-stdio`
- only uses native recovery as the preferred path

### Restart executor

Bridge restart is performed by an external executor:

```powershell
python -m imcodex ops restart --launch-snapshot <path>
```

This avoids the old "stop killed the thing that still needed to run start" trap.

## Recovery model

### What we expected from native

From upstream app-server code and tests, the ideal recovery story is:

- bridge reconnects
- bridge resumes the bound thread
- native app-server replays pending approvals
- the user can continue with `/approve` normally

### What we observed from the actual runtime binary

Using the live dedicated core and a direct probe client, we confirmed:

- same-connection `thread/resume` does replay a pending approval
- after the websocket client disconnects and a brand-new client reconnects, `thread/resume` does **not** replay the pending approval in the currently running `codex.exe`

That means:

- dedicated core still improves lifecycle separation and restart robustness
- but approval replay across disconnect is not currently reliable enough to treat as a guaranteed native recovery path

So the bridge still needs a small amount of post-reset cleanup logic. The difference is:

- this cleanup is now a fallback
- it is no longer the primary architecture

## Current modes

The code now supports these core modes:

- `dedicated-ws`
- `shared-ws`
- `auto`
- `spawned-stdio`

Recommended direction:

- production / serious local usage: `dedicated-ws`
- debugging compatibility: `spawned-stdio`

`spawned-stdio` remains in the codebase, but it should be treated as fallback/debug-only, not the architectural target.

## What this buys us

Even with the current native replay limitation, the split architecture already gives us two concrete wins:

1. Bridge restart no longer destroys core state by design.
2. Bridge hot reload can be executed externally instead of relying on the bridge to finish restarting itself after it has already stopped.

## Next step

The remaining work is to keep the dedicated-core path as the primary supported topology and gradually demote or delete bridge logic that only exists to compensate for `spawned-stdio` instability.
