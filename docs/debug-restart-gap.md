# Debug Finding: Restart Gap

## Scenario

Executed with:

```powershell
python -m imcodex debug --lab-root D:\desktop\imcodex-debug-lab scenario restart-gap --port 8016
```

Artifacts:

- `run_id`: `debug-20260419-014647`
- `instance_id`: `20260418-174647-p55168`
- `run_dir`: `D:\desktop\imcodex-debug-lab\run\debug-20260419-014647`

## Observed Result

Before stop:

- instance became `healthy`
- HTTP was listening on `8016`
- app-server connected in `spawned-stdio` mode

After stop:

- `port_listening = false`
- `auto_restarted = false`

## Evidence

Structured events showed only startup:

- `bridge.starting`
- `appserver.connect.started`
- `appserver.connect.spawn_stdio_succeeded`
- `bridge.started`

No follow-up instance appeared after stop.

## Conclusion

The current system has no automatic restart path for a stopped bridge instance.

This means:

- if a stop happens and the follow-up start never occurs
- the bridge will remain down indefinitely
- the user experience looks like "the bot died"

This supports the "restart gap / no supervisor" diagnosis.

## What This Does Not Prove

It does **not** prove that stopping the bridge also kills the Codex Desktop/TUI runtime.

What it does prove is:

- the bridge itself does not self-heal
- stop/start is not currently atomic
- a gap is enough to fully break the IM entry path

## Follow-Up Work

Recommended next steps:

1. Add a safe restart command with explicit health verification.
2. Add a supervisor or watchdog strategy for bridge processes.
3. Record stop/start operations as auditable `ops.*` events.
