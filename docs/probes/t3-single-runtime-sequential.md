# T3 Code × IMCodex shared native runtime probe

Status: **core transport GO; current T3 provider blocked by shared MCP ownership**

Date: 2026-08-10

## Decision

The first-release contract is sequential single-user input. It does not require
two clients to race `turn/start`. Native Thread, Turn, item, approval, and
terminal state remain authoritative; neither IMCodex nor the adapter may add a
second active-Turn lock or lifecycle.

T3's standard Codex provider can use a stateless stdio JSONL-to-Unix-WebSocket
adapter to reach the same official Codex App Server as IMCodex. The adapter is
only a transport translation: one stdin line becomes one WebSocket text frame,
and one text frame becomes one stdout line. It does not parse or persist native
identifiers.

This is not a full-parity T3 deployment. T3's browser-preview MCP server is
configured per provider session, while the shared App Server configuration and
environment are process-wide. The adapter's explicit chat-sync mode accepts but
does not apply the two known child settings, and locally resolves T3's exact
`config/mcpServer/reload` request so it cannot mutate the shared daemon. Preview
MCP tools are unavailable in this mode.

## Verified official Codex contract

The repository probe uses a temporary `CODEX_HOME`, workspace, Unix socket and
two adapter processes. It never connects to production T3, IMCodex, or Codex
daemon endpoints. An earlier `codex-cli 0.147.0` run with the probe's embedded
stateless adapter verified the complete sequential contract:

- both clients resume the exact same native Thread ID;
- each direction observes identical user, agent, tool, and terminal identities
  without duplicate echo;
- an active Turn survives observer disconnect/resume with one terminal result;
- a pending approval replays with the same native request ID and resolves on
  both clients;
- no concurrent `turn/start` is attempted or required.

A later run through the product adapter and launcher passed same-Thread resume,
both sequential projection directions, and active-Turn reconnect. Its first
approval attempt ended before a request appeared after repeated native error
notifications. A controlled A/B rerun then used the same isolated 0.147 server,
launch parameters, and approval scenario for the embedded and product relays.
Both received the native command approval request, replayed the same request ID
after observer reconnect, resolved it on both clients after decline, retained
no stale actionable request, and completed without producing the marker file.
This distinguishes the earlier upstream Turn failure from relay behavior.

The final isolated live gate launched the T3-side client with the exact safe
chat-sync flag and both T3-injected MCP `-c` keys. Before any Turn it issued
`config/mcpServer/reload`: the adapter returned the empty success under the
original ID, while the guard in front of the native socket observed zero reload
requests. The same run then passed both sequential projection directions,
no-echo identity checks, active reconnect, approval replay/resolution, and one
terminal snapshot on official `0.147.0`.

Run the probe against the product adapter and launcher:

```bash
.venv/bin/python scripts/probe-shared-codex-app-server.py \
  --codex-bin /Users/xbjt/.local/bin/codex \
  --expected-version 'codex-cli 0.147.0' \
  --sequential-contract \
  --shim-bin scripts/imcodex-shared-app-server \
  --t3-chat-sync-shape
```

This form launches the T3-side client with the two real MCP `-c` keys plus
`--chat-sync-without-t3-mcp`, sends one reload before the first Turn, and places
a guard socket in front of the native server. The gate fails if that reload
reaches the guard instead of being answered locally.

Run the isolated approval A/B gate:

```bash
.venv/bin/python scripts/probe-shared-codex-app-server.py \
  --codex-bin /Users/xbjt/.local/bin/codex \
  --expected-version 'codex-cli 0.147.0' \
  --shim-bin scripts/imcodex-shared-app-server \
  --approval-comparison
```

Reproduce the official cross-process writer boundary and verify recovery after
the owner exits:

```bash
.venv/bin/python scripts/probe-shared-codex-app-server.py \
  --codex-bin /Users/xbjt/.local/bin/codex \
  --expected-version 'codex-cli 0.147.0' \
  --shim-bin scripts/imcodex-shared-app-server \
  --writer-lock-recovery
```

This gate starts two isolated App Server processes with one temporary
`CODEX_HOME`. It verifies that the non-owner can read persisted state but
`thread/resume` fails with the native active-writer conflict, then stops only
the isolated owner and verifies that the same secondary connection can resume
the exact thread. It never connects to a production endpoint.

## T3 MCP parity boundary

Current T3 source performs these steps for every provider session:

1. mint a fresh bearer credential scoped to the T3 thread and provider session;
2. put the raw credential only in the spawned child's
   `T3_MCP_BEARER_TOKEN` environment variable;
3. pass the thread-specific MCP URL and bearer environment-variable name as
   `-c mcp_servers.t3-code.*` child arguments;
4. call `config/mcpServer/reload` before a Turn;
5. revoke or rotate the credential when the session stops or restarts.

A long-lived shared daemon cannot inherit a later child process's environment.
Its MCP catalog is process-wide, so a stable token would bind all native
threads to one T3 thread, while rotating global config would race other clients.
Putting raw bearer values in config or JSON-RPC would also cross the secret
boundary. The adapter therefore does not attempt either workaround.

Ignoring only the child arguments is insufficient: T3 sends
`config/mcpServer/reload` before each Turn, and that reload is process-global on
the shared daemon. `--chat-sync-without-t3-mcp` therefore intercepts that exact
request and returns `result: {}` with the original ID without forwarding it.
Source inspection of the current T3 Codex session runtime found no other
process-global config request: its remaining requests are initialize,
Thread/Turn lifecycle, read, rollback, interrupt, and start operations. Unknown,
missing, or duplicate child config remains fail-closed. The launcher also
delegates non-`app-server` calls to the real Codex binary so T3's `codex
exec`-backed title and source-control text features keep working.

## Remaining product gates

- Add a full-parity T3 external/shared provider mode or native per-thread MCP
  configuration before enabling T3 preview tools on the shared server.
- Verify in an isolated real T3 UI that a T3-mapped native thread projects an
  IM-started successful Turn and that a T3-started successful Turn reaches an
  isolated IM sink.
- Decide how an IM-created native Thread becomes a T3 project/thread record.
  Native event broadcast alone does not create T3's local UI mapping.
- Repeat approval, reconnect, and no-echo checks through that real T3 path.

No production IMCodex process, QQ channel, T3 userdata, or native daemon was
modified or restarted while producing this evidence.
