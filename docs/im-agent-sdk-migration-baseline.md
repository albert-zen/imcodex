# IM Agent SDK migration baseline

Status: baseline complete; experimental migration ready against a merged SDK revision

This document records the unchanged IMCodex baseline for the experimental
consumer migration to `im-agent-sdk`. It also proposes an ownership map for
review before code moves. It is intentionally not an SDK dependency decision.
The tracked implementation work is [Issue #28](https://github.com/albert-zen/imcodex/issues/28).

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
2. **Historical merged SDK base.** The first formal merge input was
   `c91fe8c35d714b4a325523a3240ad56163a90e65`, the PR #21 merge commit on
   `main`. It was independently fetched once by full SHA and once through
   `refs/heads/main`; both resolved to the same object. It was the base of the
   first consumer experiment, not the dependency now selected.
3. **Historical provisional dependency.** The first experiment pinned
   `im-agent-sdk[appserver,channels]` to the immutable head commit
   `66d1d91628799c6c9328e239cfe352976a4702fd` of SDK PR #22,
   [fix(appserver): harden native input outcomes](https://github.com/albert-zen/im-agent-sdk/pull/22).
   The rejected client-only trial below remains useful historical evidence,
   but this unmerged SHA is no longer the selected dependency.
4. **Current merged SDK dependency.** The branch now pins the immutable SDK
   `main` commit `17d708ec6b61767afb05e567ae7a09221ac2f364`, the merge commit of
   [PR #63](https://github.com/albert-zen/im-agent-sdk/pull/63). It contains
   the complete independently merged ADR 0015 rollout tracked by
   [issue #49](https://github.com/albert-zen/im-agent-sdk/issues/49), including
   correlation-safe continuation, bounded event fan-out and recovery gaps,
   typed I1/I2/A1/O1/O2 seams, grouped Gateway composition, metadata fidelity,
   Channel validation/diagnostics, and App Server artifact materialization.

The dependency is expressed as a PEP 508 direct Git reference in
`pyproject.toml`; it neither reads nor depends on any developer SDK worktree.
Every dependency update must resolve the remote full SHA and rerun the affected
baseline and parity evidence before runtime cutover code is accepted.

A fresh temporary environment must resolve that direct reference to
`17d708ec6b61767afb05e567ae7a09221ac2f364`, build both packages, install the
declared extras, and imported `imagent`. The full behavioral baseline remains a
gate for the implementation work rather than for publishing this handoff branch.

## Verification results

### Current SDK cutover regression

On the final SDK-composed tree, the repository regression completed with
`556 passed, 18 skipped, 1 warning`; compileall and `git diff
--check` also passed. The warning remains the third-party
`StarletteDeprecationWarning` described below. A prior run had two AgentKit
launcher subprocesses terminated by the host with `SIGKILL`; both the focused
launcher suite (`7 passed, 1 skipped`) and the unchanged full suite then passed.

The earlier Windows captures below remain the cross-platform pre-cutover
baseline; they are retained as historical comparison evidence rather than the
current test count.

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
| Multi-channel text ingress and native output | SDK native Channel contract suites cover QQ, Telegram, Feishu/Lark, and Weixin normalization/receipts; `tests/test_sdk_composition.py`, `tests/test_sdk_webhook.py`, and `tests/test_webhook_api.py` prove the consumer composition and trusted webhook route. | pass |
| Quoted-message projection | SDK native Channel suites own quote normalization and bounded metadata; consumer composition preserves SDK Metadata without a second quote/transcript path. | pass |
| Commands and Thread/workspace selection | Product grammar, native Thread query/paging, CWD-derived grouping, and native config commands remain covered by `tests/test_commands.py`, `tests/test_backend.py`, and focused Thread rendering tests. | pass |
| History and catch-up | SDK Application authoritative history/recovery contracts plus `tests/test_thread_history.py` cover bounded native reads and rendering; the old handoff projection gate was removed. | pass |
| Active/background Turn continuation | SDK Gateway/Application tests own steer-first continuation, honest `started`, correlation, and reconnect projection; consumer backend tests retain product command policy. | pass |
| Restart recovery | SDK projection/repository suites own authoritative replay and checkpoints; `tests/test_sdk_migration.py`, `tests/test_store.py`, and `tests/test_runtime.py` cover the one-time consumer state handoff. | pass |
| Timing, ordering, and deduplication | SDK App Server, Gateway, Channel, projection, and coordinator suites own bounded queues, wire order, durable admission, retry ordering, and cross-destination progress. `tests/test_webhook_api.py` proves the product webhook requests pre-media admission. | pass |
| Images and files | SDK native Channel/media suites own built-in transport media; `tests/test_channel_files.py`, `tests/test_outbound_artifacts.py`, and proactive lease tests retain product webhook/spool policy. | pass |
| Allowlist and access | Stable sender/conversation admission, `any`/`all`/deny-all behavior, rejection before media work, outbound recheck, and health labels are covered by channel foundation, middleware, config, admin, and native channel tests. This is IM admission, not Codex execution permission. | pass |
| App Server topology and trust | SDK App Server suites own transport/reconnect/dispatch contracts; `tests/test_appserver_target.py`, `tests/test_core_manager.py`, startup tests, and the real Windows smoke cover consumer topology and managed-core trust. | pass, with Unix-only cases skipped on Windows |
| Observability | Non-blocking event/log/health writers, redacted transport summaries, reconnect/degraded state, runtime lifecycle, and `health.json` are covered by `tests/test_observability.py`, `tests/test_sdk_runtime.py`, and the real Windows smoke. | pass |
| Artifact projection and sending | `tests/test_outbound_artifacts.py`, `tests/test_sdk_presentation.py`, `tests/test_sdk_delivery.py`, `tests/test_delivery_api.py`, and `tests/test_channels_send.py` cover materialization, consumer leases, typed receipts, local credentials, and proactive submission. An all-suppressed A1 attempt or lost best-effort O2 observation cannot prove safe clean-process lease release, so startup sweep is the only current recovery. | blocked |
| Approval and user input | SDK request-runtime suites own native request correlation and response safety; `tests/test_sdk_requests.py`, `tests/test_sdk_controller.py`, and command parser tests cover IMCodex presentation and product commands. | pass |
| Proactive artifact delivery | The local endpoint and `imcodex-send` use SDK proactive delivery plus the consumer path lease ledger and never expose bot credentials. Stable same-content replay and terminal lease reconciliation pass. SDK `IN_FLIGHT` crash recovery and partial retryable-suffix resumption remain unsupported, so the previous durable-outbox parity claim is not satisfied. | blocked |
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
| Explicit/proactive artifact submission | SDK typed intent, route snapshot, planner/coordinator; IMCodex authenticated wrapper, bytes/path/quota/lease policy | Keep `imcodex-send.cmd`/`CODEX_THREAD_ID` as a thin wrapper. IMCodex persists only bounded path leases, not an intent outbox. Missing SDK recovery for crash-stuck `IN_FLIGHT` submissions and partial retryable suffixes is an explicit consumer-acceptance blocker. |
| Repository tests and conformance | SDK test kit plus IMCodex product tests | Use SDK contract tests for shared seams and retain the baseline matrix for consumer behavior, topology, commands, trust, and Windows smoke. |

### Expected duplicate-removal targets

#### Current experimental cutover status

The production graph now constructs the merged SDK Gateway, Codex Application
client, and native Channels. The reusable consumer copies and their duplicate
test suites were deleted; there is no client-only compatibility composition.
The callable legacy `AppRuntime`, its raw App Server subscriptions, and its
legacy Channel lifecycle tests are deleted. `ConversationStore` has no
active-Turn cache or old outbox stage/update/ack API; it can only read and
consume already-persisted delivery evidence during the one-way SDK migration.
The retained product surfaces are:

- `src/imcodex/config.py`, `admin/`, `application.py`, `composition.py`,
  `sdk_runtime.py`, launchers, core/restart management, and branding;
- generic webhook API/media/outbound composition, channel
  registry/enablement, and Weixin login UX;
- Codex settings/Full Access commands, explicit rejection of the unavailable
  native Thread-tool host mode, and
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

1. **Proactive submission recovery remains a consumer acceptance blocker.**
   IMCodex owns a bounded crash-safe path lease ledger, but intentionally does
   not create another intent outbox. The SDK submission repository stores
   identity/outcomes rather than content. A crash can therefore leave a
   submission `IN_FLIGHT` with no SDK transition/replay path, and a `PARTIAL`
   receipt containing a retryable/unattempted suffix is terminal in the current
   SDK. O2 cannot repair either case because it is best-effort and has no retry
   authority. The consumer root artifact lease remains held while any fan-out
   destination is `IN_FLIGHT` or `RETRYABLE`; terminal mixed results are exposed
   as partial rather than total failure. Closing consumer migration still
   requires an SDK-owned recovery rule for stranded `IN_FLIGHT`, not a
   product-side coordinator.
2. **Codex workspace grouping is not an SDK Project.** Existing `/threads
   --project` groups native Threads by CWD for IM browsing. Zen/T3 may expose
   real native Projects. Keep the Codex grouping in IMCodex presentation or a
   Codex-specific controller extension; do not create SDK resource truth.
3. **Windows local-path trust is deployment evidence.** The SDK can accept an
   explicitly verified shared root/capability, but IMCodex must continue to
   prove its managed PID/listener/command/topology on every epoch. A remote or
   merely loopback second consumer is a counterexample to automatic trust.
4. **State transition must be non-destructive and explicit.** Current IMCodex
   JSON state and SDK SQLite state have different shapes. The implemented
   conversion copies bindings/routes and leaves the JSON source intact. The
   former wall-clock presentation fence was removed because it could suppress
   previously undelivered authoritative output and had a cross-store crash
   gap. Old acknowledged entries do not retain enough native item identity to
   seed an exact SDK checkpoint, so first-upgrade duplicate suppression remains
   unproven and blocks production migration acceptance.
5. **Request recovery parity.** Native Codex exposes no authoritative pending
   request snapshot. The SDK correctly refuses to reconstruct request truth
   from Gateway correlation. Baseline cases where native resume re-emits a
   request must still work, while stale handles must fail explicitly.
6. **Generic webhook and operator surfaces.** The SDK handler is not another
   web server. IMCodex must mount it inside the existing authenticated local
   service and keep webhook/admin/health exposure policy at composition.
7. **Bounded dispatch and projection pressure.** The merged SDK supplies
   bounded App Server dispatch, Gateway coordination, and request-presentation
   admission. IMCodex does not add consumer work on the socket read path and
   no longer maintains a second event dispatcher.
8. **Pre-dispatch product-command crash fencing is not exposed.** The SDK
   controller runs before the Application input side-effect fence. IMCodex
   mutating slash commands still call product/native operations from that
   controller, and a crash after the native mutation but before inbound claim
   completion can reclaim and repeat the command. The removed generic response
   cache cannot be restored as a second Channel admission/idempotency path.
   Closing migration therefore requires an SDK-owned typed operation/fence for
   these controller effects, or individually proven native idempotency for each
   command. Until then `/new`, `/fork`, `/compact`, goal/config writes, and raw
   `/native call` prevent command parity acceptance.
9. **Best-effort O2 cannot prove clean-process artifact release after observer
   loss.** IMCodex registers A1 paths by logical destination attempt and O2
   releases them on terminal outcomes. If the bounded O2 observer rejects,
   times out, or fails, the SDK exposes no terminal-attempt query that permits
   safe consumer cleanup; an independent TTL could delete a LocalPath while a
   slow logical attempt still needs it. The ledger therefore stays bounded at
   1,024 stable identities, fails explicitly at capacity, and startup sweep
   recovers leaked process-local attempts. Guaranteed clean-process convergence
   remains an SDK lifecycle-signal blocker rather than a consumer timer.
10. **SDK binding authority survives the legacy JSON handoff.** One crash-safe
    migration copies legacy thread selections only into absent SDK bindings,
    establishes one route for every current Conversation/Thread edge, and
    durably marks the handoff complete. Later startups never promote JSON
    cache or replace another Conversation's route for the same Thread. Before
    product command policy runs,
    the controller projects that SDK selection into the rebuildable product
    command context. A product command may compute a new selection before its SDK
    CAS, but a crash cannot promote that cache on restart; the next process
    restores the SDK selection instead. IMCodex selects SDK
    `foreground_only`, so an inactive historical checkpoint route never grants
    delivery authority, and implicit Thread delivery uses the SDK active-route
    set. The JSON field is no longer a second binding authority or a permanent
    split-routing shim.

No SDK Core expansion is asserted by this baseline. Proactive `IN_FLIGHT`/
partial recovery, controller-command crash fencing, and exact first-upgrade replay fencing are the remaining SDK
consumer-acceptance blockers; they cannot be repaired with O2 or a second
consumer runtime.

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

## Implementation input after SDK boundary review

The complete SDK-side rollout is merged on `main` through immutable commit
`17d708ec6b61767afb05e567ae7a09221ac2f364`; no Draft SDK branch or unpublished
worktree is an implementation input. The focused PRs and commits are recorded
in [im-agent-sdk#49](https://github.com/albert-zen/im-agent-sdk/issues/49).

The SDK change preserves normalized message Metadata through Gateway delivery,
routes live-only `message.created` observations without advancing authoritative
completion checkpoints, keeps additional Application presentation opt-in, and
adds redacted Channel lifecycle facts to the SDK diagnostics snapshot. IMCodex
continues to own visibility defaults, health-file/operator rendering, managed
artifact spool/lifetime policy, launch topology, and Full Access behavior.

Follow-up [im-agent-sdk#33](https://github.com/albert-zen/im-agent-sdk/issues/33)
is included in the merged pin. Its typed App Server presentation
hook keeps native artifact candidates in the single ordered Application
event/history path while letting IMCodex validate and materialize them into its
own managed spool. It does not move spool durability, retry, cleanup, or
visibility policy into SDK Core.

Issue [im-agent-sdk#34](https://github.com/albert-zen/im-agent-sdk/issues/34)
adds the complementary Gateway boundary: destination-specific presentation
runs only after a concrete Conversation route is selected. Suppression is a
completed, idempotent display decision, while delivery identity and destination
remain immutable. This prevents one observer's visibility preferences from
filtering every observer of the same native Thread.
The policy sees SDK-reserved live/authoritative and checkpoint facts only for
the duration of presentation; SDK strips them before durable delivery planning
so an unchanged delivery ID keeps an unchanged submission fingerprint.

Issue [im-agent-sdk#40](https://github.com/albert-zen/im-agent-sdk/issues/40)
adds a once-per-logical-delivery outcome observer. IMCodex uses it only to
release consumer-owned transient artifact leases after all SDK segments and
retries finish; the observer owns no outbox content and cannot rewrite a
delivery result. Issue
[im-agent-sdk#43](https://github.com/albert-zen/im-agent-sdk/issues/43) exposes
side-effect-free native Channel configuration validation for the existing
Windows-safe restart preflight.

IMCodex renders the SDK's redacted diagnostics snapshot into an `sdk` section
of its existing health file. The product runtime owns sampling and the overall
`healthy`/`degraded` operator status; the snapshot remains explicitly
non-authoritative and contains no native conversation, thread, turn, request,
or credential identity. Gateway startup/shutdown replaces the former manual
ordering of App Server and channel lifecycle. The default `build_runtime()` is
now cut over to this SDK composition. The product command-policy instance is
restricted to commands, bootstrap cwd/Full Access compatibility,
and staging helpers while those product boundaries are migrated; it does not
subscribe to App Server events or own normal projection/delivery lifecycle.

IMCodex imports the merged SDK App Server client, supervisor, retry policy,
protocol normalization, and redacted diagnostics directly. The transferred
consumer copies and their duplicate protocol test suite are removed; retained
`imcodex.appserver` modules contain Codex-specific command/settings/thread
policy over the SDK client only.

Proactive uploads remain consumer-owned: one bounded content-addressed spool
and crash-safe bounded lease ledger retains local paths from HTTP staging
through SDK submission. The ledger is persisted before submission, keyed by
the caller's stable delivery ID, and restored before the startup sweep so a
process crash cannot delete an attachment required by an SDK retry.
Request cleanup releases only paths that were not transferred; same-content
replay reuses the content-addressed path. The root lease remains held only
while at least one destination is `IN_FLIGHT` or `RETRYABLE`. SDK `PARTIAL` is
a terminal mixed outcome in the current submission model, so synchronous and
startup reconciliation both release the root lease once every destination is
terminal, including `ACCEPTED` + `PARTIAL` fan-out after a lost O2 callback. A
whole-attempt terminal `UNKNOWN` likewise releases every path because the SDK
will not retry it. A missing or `IN_FLIGHT` SDK record remains leased for
caller replay. This ledger is not an SDK outbox or transcript.

A1 paths are registered under the concrete destination delivery ID in O1
before Channel work and released by O2 terminal item facts. O1 suppression
does not release the shared content-addressed stager lease: O1 has no bounded
fanout-complete fact, so one suppressed route cannot safely delete a path before
another route reaches O1/Channel work. All-suppressed materializations are
therefore reclaimed by startup sweep, not an unsafe product timer.

IMCodex no longer subscribes to or journals raw App Server events. The legacy
`/native events` spelling remains only to return an explicit unavailable
diagnostic; it cannot imply that an empty local journal is authoritative.

The retired raw App Server request callback is not retained for native dynamic
Thread tools. If `IMCODEX_NATIVE_THREAD_TOOL_HOST` is explicitly enabled, the
consumer now fails startup instead of advertising tools whose calls the SDK
Application request runtime would reject or double-answer. Restoring this
optional product feature requires a future typed Application operation backed
by two real integrations; it cannot be implemented as a raw event hook during
this migration.

The consumer adapts to the merged ADR 0015 contracts without a compatibility
shim: `CodexLiveActivityPresenter` handles only bounded live activity,
`AppServerArtifactMaterializer` handles completed-item/terminal artifact facts,
O1 receives `OutboundPresentationContext`, O2 receives one typed logical
`DeliveryOutcome`, and Gateway repositories/extensions are supplied through
their immutable composition groups. No combined App Server presentation hook
or flat Gateway constructor remains in the product composition.

The product-owned generic webhook now participates in that same ownership
boundary directly: after HTTP authentication and bounded multipart parsing it
acquires the SDK Gateway admission lease, stages media, and transfers exactly
one normalized message through that lease. It does not retain the former
consumer-wide channel middleware, response cache, binding idempotency, or
delivery acknowledgement path. Immediate HTTP replies are captured only by
the webhook Channel instance, with an in-flight delivery-ID set preventing a
sink retry from appending the same immediate body twice; durable admission and
outbound identity remain SDK Gateway concerns.

The product pending-request store and `/native respond|error` escape paths are
removed. The bounded `ImcodexRequestPresenter` retains only process-local UX
handles; SDK claim/correlation state and `RespondToRequest` remain the sole
request authority.
