# IM Agent SDK migration baseline

Status: baseline complete; experimental migration in progress on a provisional stacked SDK dependency

This document records the unchanged IMCodex baseline for the experimental
consumer migration to `im-agent-sdk`. It also proposes an ownership map for
review before code moves. It is intentionally not an SDK dependency decision.

## Baseline identity

- IMCodex source: `858398226e8f76e49f8259ae686939f209e1bb36`
- Source relationship: the exact commit checked out by `main` when the
  isolated worktree was created
- Work branch: `codex/im-agent-sdk-refactor`
- Host: native Windows `10.0.26200.8875`, PowerShell `7.6.3`
- Python: CPython `3.13.7`
- Codex: `codex-cli 0.144.4`
- Capture date: 2026-08-01, Asia/Shanghai

The worktree was clean before this document was added. No IMCodex production
source, dependency metadata, state, or product configuration changed before
the baseline commands ran. The production `main` worktree was not switched or
modified.

## Candidate SDK verification versus selected dependency

These are deliberately different facts:

1. **Candidate source verification.** The remote repository
   `https://github.com/albert-zen/im-agent-sdk.git` advertised and allowed a
   fresh shallow fetch of
   `e6598335018c99257da566327fae1461c8761343`. The fetched tree and commit
   matched that SHA. The remote `codex/sdk-integration` head also resolved to
   that SHA during capture. Fetching `main` and the integration head into a
   new temporary repository showed 14 commits in the candidate integration
   range. The candidate docs and public package surface were inspected from
   that independently fetched tree, not from a developer SDK worktree.
2. **Merged SDK base.** The first formal merge input was
   `c91fe8c35d714b4a325523a3240ad56163a90e65`, the PR #21 merge commit on
   `main`. It was independently fetched once by full SHA and once through
   `refs/heads/main`; both resolved to the same object. It remains the base of
   the provisional SDK follow-up below, not the dependency selected for this
   experiment.
3. **Provisional stacked IMCodex dependency.** The experimental branch now
   pins `im-agent-sdk[appserver,channels]` to the immutable full head commit
   `66d1d91628799c6c9328e239cfe352976a4702fd` of SDK PR #22,
   [fix(appserver): harden native input outcomes](https://github.com/albert-zen/im-agent-sdk/pull/22).
   This is an unmerged, reviewable cross-repository development dependency for
   the App Server input-correctness blocker: steer dispatch-unknown handling,
   opt-in Codex continuation with native TOCTOU reconciliation, local-image
   epoch wiring, and explicit generic-file unsupported behavior. It is neither
   a floating branch reference nor a local clone/worktree dependency.

   If PR #22 changes, IMCodex must update this exact full SHA deliberately and
   rerun dependency installation plus the affected baseline/parity evidence.
   Before this IMCodex Draft PR can be treated as a production-ready change,
   the provisional SHA must be replaced by the resulting SDK `main` merge
   commit (or an explicitly released SDK version) and the full baseline matrix
   rerun. SDK PR #22 remains independently reviewed and merge-controlled.

The dependency is expressed as a PEP 508 direct Git reference in
`pyproject.toml`; it neither reads nor depends on any developer SDK worktree.
`python -m pip install -e ".[dev]"` resolved that remote Git object at the
full SHA before the experimental cutover checks below ran.

## Verification results

### Repository-defined regression

Command:

```text
python -m pytest -q --tb=short
```

Result:

```text
1171 passed, 35 skipped, 1 warning in 243.26s
```

The warning is a third-party `StarletteDeprecationWarning` from
`fastapi.testclient` about the current `httpx`/`starlette.testclient`
combination. There were no failed tests.

The first attempt used a 120-second command timeout. The runner terminated
pytest before the suite could finish and pytest then reported `OSError 22`
while flushing the closed output pipe. This was a harness timeout, not a test
failure. The successful run above used the same unchanged tree and a longer
timeout.

The 35 skips are capability/platform gates rather than hidden failures. On
this native Windows host they primarily cover Unix sockets, POSIX mode bits,
POSIX process states, and Bash/POSIX launcher cases. Windows-only command-line,
DACL, junction, launcher, and core-manager cases ran.

### Rejected client-only composition trial

After pinning SDK PR #22 at `66d1d91628799c6c9328e239cfe352976a4702fd`, a
client-only composition trial constructed the SDK `AppServerClient`,
`AppServerSupervisor`, and retry policy directly. The repeated full suite
result was:

```text
1171 passed, 35 skipped, 1 warning in 256.77s
```

The focused composition/native parity set also passed:

```text
356 passed, 8 skipped, 1 warning in 17.62s
```

It covers runtime/application lifecycle, backend command and thread behavior,
App Server transport/client behavior, and service end-to-end flows, but it did
not prove the new client against the legacy raw-notification handoff path.

A clean-context review rejected the trial before commit: SDK dispatch metadata
does not satisfy IMCodex's existing wire-order gate; SDK diagnostics are not
wired into IMCodex `health.json`/event UX; and the new SDK unknown-input
outcome cannot safely pass through the legacy backend's retry/recovery path.
The product composition therefore remains on its existing client until a real
SDK Gateway/Application cutover can preserve those semantics. No compatibility
subclass, field-name translation, or catch-and-retry shim is authorized.

### Compile, type, lint, and format facts

The repository configures pytest in `pyproject.toml`, but it does not configure
or install a project type checker, linter, or formatter in its `dev` extra or
CI. The following results therefore distinguish the repository gate from
advisory tools that happened to be installed on the capture host.

| Check | Result | Interpretation |
|---|---:|---|
| `python -m compileall -q src imcodex tests` | pass | All Python sources and tests compile on Python 3.13.7. |
| repository CI type check | not configured | No baseline type gate exists. |
| repository CI lint check | not configured | No baseline lint gate exists. |
| repository CI format check | not configured | No baseline format gate exists. |
| host `pyright src` | 492 errors | Advisory unconfigured run; primarily mixin/member and broad legacy typing gaps. |
| host `ruff check src tests imcodex` | 330 errors | Advisory unconfigured run; 157 were reported auto-fixable. |
| host `ruff format --check src tests imcodex` | 121 files would change | Advisory unconfigured run. |
| host `black --check --target-version py313 src tests imcodex` | 130 files would change | Advisory unconfigured run. |

These advisory failures are part of the baseline, not regressions introduced
by the migration. The post-migration branch must at minimum keep compile and
the repository gates green. If it adopts a real type/lint/format policy, that
policy and any legacy baseline must be explicit rather than claiming the
unconfigured host tools already passed.

### Native Windows launcher and startup

The focused launcher/startup regression command passed:

```text
python -m pytest -q --tb=short \
  tests/test_startup_scripts.py tests/test_windows_scripts.py \
  tests/test_core_cli.py tests/test_core_manager.py \
  tests/test_restart_executor.py tests/test_restart_preflight.py \
  tests/test_agentkit_launcher.py tests/test_channels_send.py \
  tests/test_app_server_cli.py tests/test_main.py

101 passed, 22 skipped in 18.73s
```

A real isolated `scripts\start.cmd` smoke also passed. It used temporary data
and run directories, dynamically allocated loopback ports, and all real IM
channels disabled:

- the launcher started a detached `codex app-server --listen ws://127.0.0.1:<port>`;
- the bridge returned `200` from `/healthz` with kind `imcodex.bridge`;
- `health.json` reported `status=healthy`,
  `appserver.status=connected`, `appserver.connected=true`, and
  `http.listening=true`;
- the instance-bound loopback shutdown endpoint returned `202`;
- Uvicorn completed application shutdown, the launcher exited `0`, and the
  project core manager stopped the exact temporary App Server;
- both temporary listeners were gone before their smoke directories were
  removed.

`scripts\doctor.ps1` also verified Python import, Codex version and
`app-server` capability, the default Windows TCP target, free HTTP port, and
channel configuration. Its only failed check was `.env`, which is intentionally
absent from this isolated worktree. No production `.env` or credential was
copied into it.

### Behavioral baseline matrix

Every row below ran inside the successful full suite. Focused test paths name
the review evidence and the parity surface to retain after migration.

| Surface | Current verified behavior and evidence | Baseline result |
|---|---|---|
| Multi-channel text ingress and native output | Raw text from QQ, Telegram, Feishu/Lark, Weixin, and the trusted webhook reaches the same native input boundary, and native final answers project back through the originating real adapter. Covered by `tests/e2e/test_multi_channel_system.py`, including `test_real_channel_ingress_reaches_native_and_projects_back_to_platform`. | pass |
| Quoted-message projection | Platform-native quote snapshots remain bounded current-input context, preserve the fact that unavailable content was quoted, reject untrusted metadata stringification/boundary forgery, and never become a local conversation-history store. Covered by quote cases in `tests/test_channels.py`, `tests/test_service_e2e.py`, and `tests/e2e/test_multi_channel_system.py`. | pass |
| Commands and Thread/workspace selection | Product grammar, native Thread query/paging, CWD-derived workspace grouping, `/pick`, attach/new/fork/name/compact, and native config commands are covered by `tests/test_commands.py`, `tests/test_backend.py`, and the command/Thread cases in `tests/test_service_e2e.py`. CWD/project grouping is presentation state; native Codex remains Thread truth. | pass |
| History and catch-up | Native `turns/list`/`thread/read`, bounded paging, active/interrupted/compacted rendering, catch-up without model work, and handoff ordering are covered by `tests/test_thread_history.py` and the history/catch-up cases in `tests/test_service_e2e.py`. No local transcript is used as model context. | pass |
| Active/background Turn continuation | Native steer-first continuation, stale-steer reconciliation, later output from attached/running Threads, and background reconnect without another inbound message are covered by `tests/test_backend.py` and `tests/test_service_e2e.py`. | pass |
| Restart recovery | Binding rehydration, stale active-Turn removal, offline completion projection, per-answer receipt retention, pending terminal delivery, and standalone-delivery restart behavior are covered by `tests/test_backend.py`, `tests/test_store.py`, `tests/test_runtime.py`, and `tests/test_service_e2e.py`. | pass |
| Timing, ordering, and deduplication | Socket-read isolation, request/notification wire order, handoff gates, response acknowledgement, lazy media after dedup, durable inbound dedup, distinct item identity, retry ordering, and cross-destination progress are covered by `tests/test_appserver_stdio.py`, `tests/test_channel_middleware.py`, `tests/test_projection.py`, and `tests/test_service_e2e.py`. | pass |
| Images and files | Static image validation/staging, generic file bounds, image-only input, native `localImage`, exact steer/start payloads, epoch-bound local-path trust, remote-path rejection, and file manifests are covered by `tests/test_channel_files.py`, `tests/test_qq_media.py`, native channel tests, `tests/test_backend.py`, and `tests/test_service_e2e.py`. | pass |
| Allowlist and access | Stable sender/conversation admission, `any`/`all`/deny-all behavior, rejection before media work, outbound recheck, and health labels are covered by channel foundation, middleware, config, admin, and native channel tests. This is IM admission, not Codex execution permission. | pass |
| App Server topology and trust | Target parsing, explicit connect-only behavior, stdio/WebSocket/Unix capability gates, bounded reconnect, dispatch overflow, Windows managed-core PID/listener/command verification, and local-image epoch trust are covered by `tests/test_appserver_target.py`, `tests/test_appserver_stdio.py`, `tests/test_core_manager.py`, startup tests, and the real Windows smoke. | pass, with Unix-only cases skipped on Windows |
| Observability | Non-blocking event/log/health writers, redacted transport summaries, reconnect/degraded state, runtime lifecycle, and `health.json` are covered by `tests/test_observability.py`, `tests/test_runtime.py`, and the real Windows smoke. | pass |
| Artifact projection and sending | Explicit artifact extraction/staging, per-artifact receipts, partial/permanent failure, durable outbox, sender launcher selection, current-Thread route resolution, loopback credential boundary, idempotent replay, and restart are covered by `tests/test_outbound_artifacts.py`, `tests/test_channel_artifacts.py`, `tests/test_delivery_api.py`, `tests/test_channels_send.py`, and `tests/test_service_e2e.py`. | pass |
| Approval and user input | Native request IDs, batch and prefix approval, plain-text cancellation, bounded request delivery, explicit rejection of unsupported requests, permission-profile responses, and structured answers are covered by `tests/test_commands.py`, `tests/test_appserver_stdio.py`, and `tests/test_service_e2e.py`. | pass |
| Proactive artifact delivery | The local delivery endpoint and `imcodex-send` route through the same durable outbox, preserve per-artifact outcome, bind implicit delivery to native `CODEX_THREAD_ID`, and do not expose bot credentials. Covered by delivery API, sender, store, and service end-to-end tests. | pass |
| Contract/schema/architecture | App Server generated-request schema drift and the repository dependency rules are covered by `tests/test_appserver_schema_drift.py` and `tests/test_architecture.py`; the full test suite included both. | pass |

### Environment limits

- No real QQ, Telegram, Feishu/Lark, Weixin, webhook gateway, or remote App
  Server credential was supplied. Live external delivery and provider-side
  rate limiting were not exercised. The full offline contract/system suite
  uses real adapters and bridge code with deterministic endpoint and native
  protocol doubles.
- Unix control-socket, POSIX permission, Bash launcher, and POSIX process-state
  cases cannot run natively on this Windows host. CI must continue to provide
  Ubuntu coverage while Windows CI covers the native launcher and process APIs.
- The isolated worktree has no `.env`; the real Windows smoke intentionally
  used process-local, non-secret configuration and disabled external channels.
- No destructive state migration was attempted. Production bridge state was
  neither opened nor copied.

## Proposed migration ownership map

The target is a thin IMCodex product/composition layer. Native Codex remains
authoritative for Threads, Turns, items, requests, history, model, reasoning,
permissions, and execution. SDK ownership does not make the SDK an Agent
runtime or policy authority.

| Current concern | Proposed owner | Migration disposition |
|---|---|---|
| Language-neutral Application/Project/Thread/message/operation/event contracts | SDK Core | Consume SDK contracts; delete equivalent IMCodex-neutral DTOs after parity. Do not represent Codex workspace grouping as a synthetic native Project. |
| Conversation binding, projection routes, bridge idempotency, reply/request correlations | SDK Gateway + persistence | Move to SDK repositories/runtime where semantics match; retain only IMCodex migration/bootstrap wiring. Never store transcript, Turn, approval, model, or permission truth. |
| Live fan-out, authoritative catch-up/recovery, projection checkpoints | SDK projections/recovery plus a proven bounded consumer boundary | Use SDK projection/recovery only after baseline-comparative tests prove the full pressure and restart boundary. Native Codex reads remain authoritative. The candidate's Application subscriber/bootstrap queues are documented as unbounded, so existing bounded dispatch/admission/overflow reconciliation must not be deleted until the final composition supplies an equivalent bounded stage. |
| Capability planning, per-destination FIFO, bounded sends, typed receipts | SDK delivery planner/coordinator | Use the common planner/coordinator; remove duplicate generic segmentation/coordination. IMCodex chooses capacity and retry appetite. |
| QQ, Telegram, Feishu, and Weixin transports, protocol media helpers, and platform receipts | SDK Channel adapters | Construct SDK adapters from IMCodex settings; delete transferred IMCodex production copies after parity. |
| Stable-ID access mechanism | SDK Channel adapter mechanism | SDK may implement the adapter-native gate; IMCodex retains the configured allowlists, match policy, operator UX, and health presentation. |
| Codex JSON-RPC client, target model, retry, protocol classification, redacted diagnostics, supervisor | SDK Codex Application/client adapter | Consume the SDK implementation and delete duplicate reusable client modules after protocol/epoch/reconnect parity. |
| Codex resource/input/event/history/request translation | SDK Codex Application adapter | Use typed SDK operations and events. Native request handles and Turn lifecycle remain Codex-owned. |
| Common Slash navigation/history/catch-up/request presentation | Optional SDK Controller | Reuse only the common subset. IMCodex keeps its product grammar, aliases, branded rendering, and Codex-only extensions. |
| Generic trusted webhook HTTP ingress and outbound callback composition | IMCodex product/composition | Keep as a product surface unless a second real consumer proves a common adapter. It mounts SDK Gateway calls but is not SDK Core. |
| `.env`, `Settings`, admin console, secret-preserving config writes | IMCodex product | Keep. Native settings continue to be read/written through Codex rather than shadowed locally. |
| Windows `start.cmd`/PowerShell launchers, doctor, detached core manager, restart executor | IMCodex product/runtime | Keep. They express the product's deployment topology and operational recovery. |
| Managed Windows TCP App Server trust verification | IMCodex deployment policy feeding an SDK capability | Keep manifest/PID/listener/live-command verification in IMCodex; pass only an explicitly verified shared-filesystem capability/root into the SDK adapter. |
| Branding, admin assets, user-facing IMCodex wording | IMCodex product | Keep entirely out of SDK Core. |
| Codex-only `/model`, `/think`, `/personality`, `/fast`, `/credits`, `/goal`, `/native`, `/config`, Thread-tool hosting | IMCodex consumer/Application policy | Keep as thin native operations. Do not add them to common SDK Slash semantics. |
| Full Access defaults and `/permission` product behavior | IMCodex consumer policy over native Codex config | Keep. SDK translates native requests but never chooses Full Access, sandbox, approval, or prompting policy. |
| Runtime health files, bridge log/events, `/status`, and operator diagnostics | IMCodex composition, using SDK diagnostics/worker health | Reuse SDK redacted facts and health; keep file layout, HTTP exposure, labels, and operator UX in IMCodex. Logging must remain off the socket read path. |
| Explicit/proactive artifact submission | SDK typed intent, handler, route snapshot, planner/coordinator; IMCodex durable product boundary, wrapper, and hosting | Mount the SDK handler in the existing authenticated local service and keep `imcodex-send.cmd`/`CODEX_THREAD_ID` product UX as a thin wrapper. The candidate handler is synchronous and is not a durable job/content store, so IMCodex must retain or replace with proven parity its durable outbox, managed artifact spool lifetime, and final per-artifact acknowledgement ledger across restart. Do not give callers bot credentials or persistence access. |
| Repository tests and conformance | SDK test kit plus IMCodex product tests | Use SDK contract tests for shared seams and retain the baseline matrix for consumer behavior, topology, commands, trust, and Windows smoke. |

### Expected duplicate-removal targets

#### Current experimental cutover status

No production App Server client deletion or replacement is accepted yet. The
legacy `src/imcodex/appserver/client.py` remains production-constructed until
SDK Gateway/Application composition has demonstrated wire-order, native
unknown-input recovery, and observability parity. Its bounded dispatch
behavior remains evidence for the later SDK queue/projection admission
decision. This is a tracked blocker, not permission for an indefinite shim or
for copying IMCodex semantics into SDK Core.

After parity, the reusable implementations already transferred to the SDK
should disappear from IMCodex rather than remain as indefinite shims:

- `src/imcodex/channels/access.py`, `artifacts.py`, `base.py`, `media.py`,
  `qq_media.py`, `qq.py`, `telegram.py`, `feishu.py`, `weixin_ilink.py`,
  `weixin_state.py`, `weixin.py`, and neutral text/file/security helpers;
- `src/imcodex/app_server_target.py` and reusable parts of
  `src/imcodex/appserver/client.py`, `diagnostics.py`, `protocol_map.py`,
  `retry.py`, and `supervisor.py`;
- generic binding/projection/delivery coordination that is fully provided by
  the SDK Gateway, persistence, recovery, planner, and coordinator.

The following adjacent code remains product-owned or must become thin
composition rather than being copied into SDK Core:

- `src/imcodex/config.py`, `admin/`, `application.py`, `composition.py`,
  `runtime.py`, launchers, core/restart management, and branding;
- webhook API/outbound composition, channel registry/enablement, and Weixin
  login UX;
- Codex settings/Full Access commands, native Thread-tool host policy, and
  product-specific command rendering;
- IMCodex observability file/HTTP surfaces and the `imcodex-send` wrapper.

## Multi-consumer boundary review

Every possible gap must be classified before asking the SDK to grow.

### 1. Core invariant

An invariant belongs in SDK Core only when a second Channel/Application needs
the same semantic rule. Examples already supported by the candidate are stable
typed identity, honest delivery receipts, per-Conversation serialization,
binding/route separation, native-authoritative recovery, and the rule that a
completed message does not imply Turn completion.

Counterexample test: T3 Code or Zen must be able to use the invariant without
learning a Codex request method, Codex Full Access profile, IMCodex command, or
Windows core manifest.

### 2. Optional capability

Replay, native activation, interactive requests, local-path attachments,
proactive delivery, and common Slash UX are optional capabilities. An
Application or Channel that cannot prove one must advertise it as unsupported
rather than receiving a compatibility fiction.

Counterexample: a remote T3 Application may support Thread input/history but
not a bridge-local filesystem path; the SDK must not infer attachment trust
from connectivity.

### 3. Adapter-specific policy

Native escaping, platform limits, credentials, reconnect tokens, media
download/upload, and receipt interpretation belong to Channel adapters. Codex
wire mapping, connection epochs, and native request response shapes belong to
the Codex Application adapter.

Counterexample: Telegram retry-after and Codex `localImage` encoding have no
meaning for a Feishu/T3 pairing, so neither belongs in common Gateway rules.

### 4. Consumer policy

IMCodex branding, command aliases, CWD-derived workspace grouping, allowlist
values, Full Access defaults, Windows launch topology/trust, admin console,
observability layout, retry appetite, and implicit `CODEX_THREAD_ID` sender UX
remain consumer policy.

Counterexample: a second SDK consumer could expose native Zen Projects, use a
remote App Server, choose approval-on-request, publish Prometheus health, and
disable proactive delivery. Making the IMCodex choices Core would prevent that
valid consumer.

## Boundary issues to resolve after the final SDK pin

These are migration review items, not authorization to expand the SDK:

1. **Terminal/proactive outbox versus honest unknown outcomes.** IMCodex currently keeps
   exact terminal/answer-segment deliveries retryable across restart until its
   sink accepts them. Proactive submissions use that same outbox, managed
   artifact spool, and final per-artifact acknowledgement ledger. The candidate
   SDK handler is synchronous, intentionally stores neither job content nor
   artifact bytes, and does not automatically retry an ambiguous native
   outcome. Stable identity and honest `unknown` are Core invariants;
   IMCodex's durable persistence, spool lifetime, and retry appetite are
   consumer policy unless a second consumer proves a common optional
   capability. The migration must preserve both terminal and proactive
   receipts across restart and avoid duplicate sends without hiding ambiguity.
2. **Codex workspace grouping is not an SDK Project.** Existing `/threads
   --project` groups native Threads by CWD for IM browsing. Zen/T3 may expose
   real native Projects. Keep the Codex grouping in IMCodex presentation or a
   Codex-specific controller extension; do not create SDK resource truth.
3. **Windows local-path trust is deployment evidence.** The SDK can accept an
   explicitly verified shared root/capability, but IMCodex must continue to
   prove its managed PID/listener/command/topology on every epoch. A remote or
   merely loopback second consumer is a counterexample to automatic trust.
4. **State transition must be non-destructive and explicit.** Current IMCodex
   JSON state and candidate SDK SQLite state have different shapes. Only
   bridge-owned bindings, routes, delivery identity/receipts, and IM reply
   correlations are eligible to migrate. No transcript or native lifecycle
   truth may be imported. A one-time conversion must keep a recoverable backup
   or the experiment must use isolated state; no production state migration is
   authorized by this baseline.
5. **Request recovery parity.** Native Codex exposes no authoritative pending
   request snapshot. The SDK correctly refuses to reconstruct request truth
   from Gateway correlation. Baseline cases where native resume re-emits a
   request must still work, while stale handles must fail explicitly.
6. **Generic webhook and operator surfaces.** The SDK handler is not another
   web server. IMCodex must mount it inside the existing authenticated local
   service and keep webhook/admin/health exposure policy at composition.
7. **Bounded dispatch and projection pressure.** The candidate SDK explicitly
   documents unbounded Application subscriber and bootstrap queues. IMCodex
   requires a fast socket read path, bounded server-request/notification
   dispatch, explicit overflow reset/reconciliation, and bounded IM request
   presentation. The transferred App Server client covers part of this
   boundary, but the final composition must prove there is no unbounded gap
   between Application publication and Channel coordination. Until that proof
   exists, removal of the current bounded consumer stage is blocked. A second
   consumer with a different event volume may choose different limits, so the
   numeric policy stays consumer-owned even if a reusable optional bounded
   capability is eventually justified.

No SDK Core expansion is asserted by this baseline. Durable proactive parity
and end-to-end bounded pressure are implementation blockers until the final
immutable SDK input and IMCodex composition are inspected and tested. That
review must decide whether each is existing SDK capability, thin consumer
glue, or a genuinely reusable optional capability before proposing any SDK
change.

## Post-pin validation gate

Implementation must not be declared complete until it compares against this
baseline and passes:

- SDK unit/contract/schema/type/lint/format/compile/build checks at the final
  pin;
- IMCodex full pytest and compile checks, plus any newly adopted explicit type,
  lint, and format policy;
- the real isolated Windows launcher/startup/graceful-shutdown smoke;
- multi-channel text ingress/native output and quoted-message projection;
- command and Thread/workspace navigation, history/catch-up, active/background
  continuation, reconnect/restart recovery, timing/dedup, images/files,
  allowlist/access, App Server topology/trust, observability, approvals/user
  input, artifact projection, and proactive artifact delivery, including its
  durable spool and per-artifact acknowledgement state across restart;
- bounded socket dispatch, Application projection pressure, coordinator
  admission, overflow reconciliation, and request-presentation behavior;
- clean-install proof that IMCodex imports SDK-owned implementations and no
  migrated duplicate production implementation remains;
- a clean-context architecture/recovery/security review.

The final branch may be pushed only as
`codex/im-agent-sdk-refactor`, and the final pull request must remain Draft.
Nothing in this baseline authorizes merging.
