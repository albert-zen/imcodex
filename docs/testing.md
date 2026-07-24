# Testing

## Purpose

IMCodex has a system-level contract suite in addition to focused unit and
component tests. Its job is to verify the boundary that matters to users:

```text
platform event
  -> real channel adapter
  -> unified channel middleware
  -> bridge
  -> native App Server protocol
  -> projected native event
  -> real channel adapter
  -> platform request
```

The suite is deterministic and offline. It does not contact QQ, Telegram,
Feishu/Lark, Weixin, a generic webhook receiver, or a model. Instead, it keeps
the production parsers, middleware, bridge, App Server client, protocol
mapping, projection, and outbound adapters in the path while replacing the
external network endpoints and native model execution.

For built-in-channel media cases, the authenticated platform download is
represented by a deterministic accepted materialization result so the system
scenario can focus on adapter normalization and native input mapping. The
generic webhook case runs the real shared byte validator and spool. Decoder,
quota, expiry, cancellation, and invalid-media behavior for every built-in
adapter remain covered by focused media tests.

## System Harness

The reusable harness lives in `tests/e2e/system_harness.py`.

`ScriptedNativeProcess` speaks the same JSONL request/response and notification
shape as a bridge-child Codex App Server. A test queues native responses by
method, observes the exact requests emitted by IMCodex, and can attach native
notifications to a response. Unscripted native requests fail the scenario,
and every queued response must be consumed.

`SystemHarness` wires the real objects together:

- `AppServerClient`
- `CodexBackend`
- `BridgeService`
- `UnifiedChannelMiddleware`
- `MultiplexOutboundSink`
- `ConversationStore`

It deliberately does not implement another thread, turn, model, or projection
runtime. Native protocol steps remain explicit test inputs.

The harness matches the bridge-child App Server topology by enabling the
experimental native protocol capability and registering the production
thread-management dynamic tools on newly created threads. It does not run the
composition root's startup permission-default initialization; focused runtime
and configuration tests own that ready-hook contract.

## Coverage Matrix

`tests/e2e/test_multi_channel_system.py` covers:

| Contract | QQ | Telegram | Feishu/Lark | Weixin | Generic webhook |
|---|---:|---:|---:|---:|---:|
| Raw text ingress reaches `turn/start` | Yes | Yes | Yes | Yes | Yes |
| Native final answer reaches platform egress | Yes | Yes | Yes | Yes | Yes |
| Raw image ingress becomes `localImage` | Yes | Yes | Yes | Yes | Yes |
| Raw generic file becomes a readable manifest | Yes | Yes | Yes | Yes | Yes |
| Native quote snapshot reaches current input | Yes | N/A | N/A | N/A | N/A |

The shared product workflow scenario enters through a real Telegram update and
verifies:

- the native thread panel (`/threads`)
- switching to a native thread (`/pick`)
- continuing the selected thread
- native credits and usage reads (`/credits`)
- creating and binding a new native thread (`/new`)

These command semantics live in the shared bridge, so they are exercised once
through a real transport rather than duplicated across every adapter. Adapter
parity is enforced by the transport matrix above.

Focused tests remain responsible for failure injection, retries, access
policy, reconnect, deduplication, media validation limits, durable delivery,
history pagination, approvals, and protocol drift. The system suite complements
those tests; it does not replace them.

## Running

Run only the system contract suite:

```bash
python -m pytest -q tests/e2e
```

Run the complete regression suite:

```bash
python -m pytest
```

Both commands require only development dependencies. No channel credentials,
Codex installation, model quota, or external network access are required.

## Extending

When adding a channel:

1. Feed a platform-native event into its production parser/handler.
2. Keep the production middleware, bridge, App Server client, and outbound
   adapter in the scenario.
3. Mock the platform SDK/HTTP boundary and native App Server process. A
   built-in media case may inject an accepted materialization result when
   authenticated download behavior is already covered by focused adapter
   tests; keep the raw media reference parser and native input mapping real.
4. Assert both the exact native request and the platform-native outbound
   request.
5. Add text, image, and generic-file cases for every capability the channel
   advertises.

When adding a shared command or thread operation, extend the shared workflow
scenario unless the behavior is genuinely channel-specific.
