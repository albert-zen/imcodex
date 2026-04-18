# Logging And Observability

`imcodex` now keeps per-instance runtime archives under `.imcodex-run` instead of
overwriting a single stdout/stderr file on every restart.

## Goals

The current design optimizes for local debugging and restart forensics:

- each bridge process gets a stable `instance_id`
- archived runs are kept under `runs/`
- the latest process state is mirrored under `current/`
- lifecycle, channel, and app-server state changes are written as structured events
- health is tracked separately from text logs

This is intentionally a local-first observability layer. It is not a remote log
aggregation system.

## Layout

By default the runtime root is `.imcodex-run/`.

Each instance writes:

```text
.imcodex-run/
  current/
    instance.json
    health.json
    bridge.log
    events.jsonl
  runs/
    20260419-101530-p48648/
      instance.json
      health.json
      bridge.log
      events.jsonl
```

The `runs/<instance_id>/` directory is immutable instance history.

The `current/` directory mirrors the active instance so operators do not need to
find the newest archive path before checking status.

## Core Files

### `instance.json`

Static metadata written once at startup.

Includes:

- `instance_id`
- `pid`
- `started_at`
- `service_name`
- `cwd`
- `git_branch`
- `git_commit`
- `python_version`
- `http_host`
- `http_port`
- `app_server_url`

Use this file to answer:

- which version was running?
- which PID owned this run?
- what port and app-server mode was configured?

### `bridge.log`

Human-readable log file with instance-aware formatting:

```text
2026-04-19 10:15:30 INFO [20260419-101530-p48648] imcodex.channels.qq QQ gateway ready session_id=session-1
```

This is meant for quick local inspection.

### `events.jsonl`

Structured event stream, one JSON object per line.

Representative fields:

- `ts`
- `level`
- `component`
- `event`
- `message`
- `instance_id`
- `pid`
- optional `data`

Example event names currently emitted:

- `bridge.starting`
- `bridge.started`
- `bridge.start_failed`
- `bridge.stopping`
- `bridge.stopped`
- `qq.gateway.connecting`
- `qq.gateway.ready`
- `qq.gateway.resumed`
- `appserver.connect.started`
- `appserver.connect.shared_ws_succeeded`
- `appserver.connect.spawn_stdio_succeeded`
- `appserver.connection.closed`

Use `events.jsonl` when you need precise ordering or want to grep for a specific
event family.

### `health.json`

Latest known runtime status snapshot.

Current shape:

```json
{
  "instance_id": "20260419-101530-p48648",
  "status": "healthy",
  "http": {
    "listening": true,
    "host": "0.0.0.0",
    "port": 8000
  },
  "channels": {
    "qq": {
      "connected": true,
      "session_id": "..."
    }
  },
  "appserver": {
    "connected": true,
    "mode": "shared-ws"
  },
  "updated_at": "2026-04-19T10:15:31+08:00"
}
```

Use this file to answer:

- is the bridge up?
- is the HTTP listener marked ready?
- is QQ connected?
- is the app-server connected, and through which transport?

## Configuration

The runtime root can be overridden with:

```text
IMCODEX_RUN_DIR=.imcodex-run
```

If unset, the default is `.imcodex-run`.

## Retention

Archived runs are pruned automatically. The current implementation keeps the
most recent archived instance directories and removes older ones when the
runtime starts.

This prevents local build-up while still preserving the most recent crash and
restart history.

## Design Notes

Observability is implemented as a unified cross-cutting concern under
`src/imcodex/observability/`.

Other layers should:

- use normal `logging.getLogger(__name__)`
- emit structured events through the shared observability hooks
- update health through the shared health hooks

Other layers should not:

- open their own log files
- define custom JSON event formats
- manage per-instance run directories themselves

This keeps logging semantics stable across `bridge`, `channels`, and
`appserver`.
