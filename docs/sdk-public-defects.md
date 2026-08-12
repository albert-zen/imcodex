# Public SDK acceptance findings

The acceptance runtime uses the exact SDK artifact recorded in
[`sdk-provenance.md`](sdk-provenance.md).  IMCodex does not patch or import SDK
Core internals.

## New Codex thread foreground binding

With `stdio://` and the public `CodexApplicationAdapter` plus `Gateway` APIs:

1. `codex_app_server_client(...).start_thread(cwd=...)` returns a native thread.
2. `CodexApplicationAdapter.execute(GetThread(...))` reads that thread successfully.
3. `Gateway.actions(...).create_thread(...)` succeeds.
4. `Gateway.actions(...).create_and_bind_thread(...)` succeeds with foreground
   projection enabled without requesting a native turn list for the empty thread.
5. A first ordinary input is accepted and produces one projected result.

The canonical `a72b24a` artifact fixes the prior empty-thread defect by
preserving delayed native thread-start evidence through foreground route
reconciliation.  The public SDK native suite passes (`1184 passed, 2772
subtests passed`), including restart/replay coverage that recovers foreign
output once and replays the completed create/bind action without duplication.

The installed wheel was verified from its `direct_url.json` and has SHA-256
`e160c31dd5661c9b419193c9ad3ddb1deb676949fc4a730ed36c65710885572d`.  The
IMCodex regression suite passes (`299 passed, 14 skipped`).  Ten independent
fresh real `stdio://` IMCodex processes were also run with the Luna model at
reasoning effort `high` and the native service tier left unchanged; each
produced exactly one `IMCODEX_ACCEPTANCE_OK` output.

No executable public SDK defect remains open for this acceptance path.

## Downstream consumer parity corrections

The public SDK already exposes the primitives required for the following
IMCodex behavior.  The SDK experiment must consume those primitives directly:

- `/view minimal|standard|verbose` updates the effective commentary, tool-call,
  and system visibility switches, not only the persisted profile label.
- `/permission default|read-only|full-access` writes `approval_policy` and
  `sandbox_mode` together and reloads the effective native configuration.
- standalone current-thread delivery uses `ThreadRouteDeliveryTarget` with a
  thread-scoped `DeliveryPrincipal`; composition uses
  `REMEMBERED_LAST_RECIPIENT` so the route survives selecting another thread
  and moves when the same thread is selected from another conversation.

These were IMCodex consumer omissions, not public SDK defects.  The focused
consumer coverage and full regression suite pass against canonical commit
`a72b24a` (`300 passed, 26 skipped`).  Durable payload/artifact retry remains a
separate IMCodex outbox responsibility because the SDK persists submission
identity and routing state, not consumer payload bytes.

Other deliberate unsupported behavior remains explicit: the fixed Codex
workspace does not support per-conversation `/cwd` changes, native thread tool
hosting is rejected before composition, and the Codex adapter does not
advertise path-based attachment input in its v1 capabilities.
