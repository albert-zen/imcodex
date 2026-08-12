# Public SDK acceptance findings

The acceptance runtime uses the exact SDK artifact recorded in
[`sdk-provenance.md`](sdk-provenance.md).  IMCodex does not patch or import SDK
Core internals.

## New Codex thread foreground binding

With `stdio://` and the public `CodexApplicationAdapter` plus `Gateway` APIs:

1. `codex_app_server_client(...).start_thread(cwd=...)` returns a native thread.
2. `CodexApplicationAdapter.execute(GetThread(...))` reads that thread successfully.
3. `Gateway.actions(...).create_thread(...)` succeeds.
4. `Gateway.actions(...).bind_thread(...)` or `create_and_bind_thread(...)` returns
   `Partial(ActionErrorCode.NATIVE_REJECTED,
   OperationErrorCode.ADAPTER_FAILURE)` when foreground projection is enabled.

The Codex App Server rejects `thread/turns/list` for the newly-created,
unmaterialized thread until its first user message.  SDK foreground route
reconciliation requests that history before dispatching the first user input.
The same behavior is observable through the public SDK workflow and leaves the
binding persisted, so IMCodex reports the typed partial outcome rather than
creating a local projection or bypassing Gateway.

This is an executable SDK defect: the SDK must defer empty-thread history
reconciliation (or otherwise admit the first input) without requiring a native
turn list before the first message.  Until fixed upstream, the real vertical
path is healthy through ingress and product commands such as `/help`, but an
ordinary first text turn is explicitly blocked by this public SDK outcome.

Other deliberate unsupported behavior remains explicit: the fixed Codex
workspace does not support per-conversation `/cwd` changes, native thread tool
hosting is rejected before composition, and the Codex adapter does not
advertise path-based attachment input in its v1 capabilities.
