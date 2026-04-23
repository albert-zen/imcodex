# Rewrite Context Research

Status: Draft

Last updated: 2026-04-19

Codex upstream snapshot used for this research:

- repository: `D:\desktop\codex-upstream`
- branch: `main`
- commit: `996aa23e4ce900468047ed3ec57d1e7271f8d6de`

This document summarizes the context we need before rebuilding `imcodex`.

It is not the product behavior spec and not the implementation spec.
Its purpose is to answer:

- what facts about the IM side we are intentionally keeping
- what native capabilities Codex App Server already exposes
- what native events and requests Codex can emit
- what local bridge state is truly unavoidable
- what risks and edge cases the rewrite must account for

## 1. Scope Of The Rewrite

The current rewrite target is still the same product shape:

- IM adapters stay broadly the same as the current product
- Codex remains the native authority for threads, turns, items, models, permissions, and reasoning effort
- the bridge should become thinner, not thicker

The accepted product behavior is defined separately in:

- [product-behavior-spec.md](product-behavior-spec.md)
- [system-constraints-spec.md](system-constraints-spec.md)
- [0001-native-thin-bridge.md](adr/0001-native-thin-bridge.md)

This document exists to make the rewrite team stop guessing about Codex.

## 2. IM-Side Facts We Are Keeping

The user explicitly asked that the IM side remain close to the current product.
So the rewrite should preserve these facts unless we consciously decide otherwise.

### 2.1 Unified IM message shapes already exist

Current `imcodex` already has simple common message models:

- `InboundMessage`
- `OutboundMessage`

Source:

- `src\imcodex\models.py`

The current fields are already close to the right minimum:

- `InboundMessage`
  - `channel_id`
  - `conversation_id`
  - `user_id`
  - `message_id`
  - `text`
  - `reply_to_message_id`
  - optional trace/timestamp fields
- `OutboundMessage`
  - `channel_id`
  - `conversation_id`
  - `message_type`
  - `text`
  - `request_id`
  - `metadata`

Conclusion:

- the rewrite does not need a complex IM-side domain model
- these two common message shapes are enough for `Adapter <-> Bridge`

### 2.2 IM conversations are keyed by platform plus conversation id

Current QQ behavior uses:

- `channel_id = "qq"`
- `conversation_id = "c2c:<openid>"` for direct messages
- `conversation_id = "group:<group_openid>"` for group messages

Source:

- `src\imcodex\channels\qq.py`

Conclusion:

- the bridge must continue to identify an IM conversation by `(channel_id, conversation_id)`
- the bridge does not own the IM platform identity model; adapters do

### 2.3 Reply metadata is unavoidable

Current adapters need IM-specific reply anchors such as:

- `reply_to_message_id`
- last inbound message id

Conclusion:

- minimal IM reply metadata is legitimate bridge state
- this is adapter-driven state, not Codex truth

### 2.4 Current product behavior to preserve

The current accepted product behavior is:

- `/help`
- `/cwd`
- `/new`
- `/threads`
- `/pick` / `/next` / `/prev` / `/exit` inside thread browser
- `/status`
- `/stop`
- `/model`
- `/permission`
- normal text always triggers Codex work
- default visibility shows commentary and final, hides tool calls
- default mode is effectively full-access
- approval UX exists for non-default situations

Conclusion:

- the rewrite is not inventing a new product
- it is re-implementing the same product on cleaner boundaries

## 3. Codex Native Mental Model

The latest local upstream and the official OpenAI article point to the same core model:

- `Thread`
- `Turn`
- `Item`

Codex App Server is the native surface that exposes these concepts to clients.

Important consequence:

- `imcodex` should not build a second truth for sessions or agent state
- it should route IM conversations into native Codex threads and project native output back into IM

Primary local source:

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`

Official source:

- [Unlocking the Codex harness: how we built the App Server](https://openai.com/index/unlocking-the-codex-harness/)

## 4. Codex Transport And Lifecycle Facts

### 4.1 App Server is the main integration surface

The official OpenAI article explicitly frames App Server as the first-class integration method for Codex.

The local upstream README shows the app-server as a long-lived process exposing a JSON-RPC event/request surface.

Conclusion:

- the rewrite should target native App Server directly
- do not rebuild around CLI scraping or ad hoc process parsing

### 4.2 Core lifecycle

The standard lifecycle in current upstream is:

1. initialize
2. `thread/start` or `thread/resume`
3. `turn/start`
4. stream notifications while the turn runs
5. `turn/completed`

There are also control operations such as:

- `turn/steer`
- `turn/interrupt`
- `thread/fork`
- `thread/list`
- `thread/read`

Source:

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`

### 4.3 Transport caveat

The upstream README still documents stdio/JSONL as the normal App Server framing and treats websocket support as experimental/unsupported.

Conclusion:

- transport choice must be treated as an explicit engineering decision
- if we keep websocket in our environment, we should treat it as a compatibility risk rather than assuming it is the canonical path

### 4.4 Websocket backpressure is not theoretical

During direct debugging of a real `imcodex` stall, we reproduced a concrete websocket backpressure failure:

1. Codex emitted a high-volume stream of notifications during a turn.
2. `imcodex` consumed them too slowly.
3. App Server logged `disconnecting slow connection after outbound queue filled`.
4. Subsequent outbound messages for that connection were dropped.
5. The bridge was left with stale local turn state until the next user message triggered reconnect/recovery.

Relevant upstream facts:

- websocket outbound writes use bounded channels
- websocket connections are disconnectable on full outbound queues
- stdio transport has a different behavior and waits instead of disconnecting in the tested full-queue path
- upstream queue capacity is currently `128`

Primary local sources:

- `D:\\desktop\\codex-upstream\\codex-rs\\app-server\\src\\transport\\mod.rs`
- `D:\\desktop\\codex-upstream\\codex-rs\\app-server\\src\\transport\\websocket.rs`
- `D:\\desktop\\codex-upstream\\codex-rs\\app-server\\README.md`

Conclusion:

- "consume everything then decide what to show" is still correct at the architecture level
- but in websocket mode we must also care about native backpressure and notification volume
- the bridge cannot afford a slow, synchronous read path
- native opt-out of unnecessary high-volume notifications is part of a correct integration, not an optional optimization

## 5. Native Codex Operations We Need For The Product

These are the native APIs that matter directly to the current product behavior.

### 5.1 Thread creation and switching

Relevant native methods:

- `thread/start`
- `thread/resume`
- `thread/fork`
- `thread/list`

Facts confirmed from the latest upstream:

- `thread/start` accepts `cwd`
- `thread/start` also accepts thread-level overrides such as model, approval policy, sandbox, config, personality, and reviewer
- `thread/resume` reopens a thread by `threadId`
- `thread/list` supports pagination and filtering

Current `ThreadStartParams` includes:

- `model`
- `modelProvider`
- `serviceTier`
- `cwd`
- `approvalPolicy`
- `approvalsReviewer`
- `sandbox`
- `config`
- `serviceName`
- `baseInstructions`
- `developerInstructions`
- `personality`
- `ephemeral`
- `sessionStartSource`

Current `ThreadListParams` includes:

- `cursor`
- `limit`
- `sortKey`
- `sortDirection`
- `modelProviders`
- `sourceKinds`
- `archived`
- `cwd`
- `searchTerm`

Current `ThreadStartResponse` / `ThreadResumeResponse` already return:

- `thread`
- `model`
- `modelProvider`
- `serviceTier`
- `cwd`
- `instructionSources`
- `approvalPolicy`
- `approvalsReviewer`
- `sandbox`
- `reasoningEffort`

Conclusions:

- `/new` should be implemented with native `thread/start`
- `/threads` should be implemented with native `thread/list`
- thread switching should use native `thread/resume` rather than bridge-owned fake thread state
- thread switch success messages should read native `cwd` back from the response

Primary local sources:

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadStartParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadListParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadStartResponse.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadResumeResponse.ts`

### 5.2 Running Codex

Relevant native methods:

- `turn/start`
- `turn/steer`
- `turn/interrupt`

Facts confirmed from upstream:

- normal user text should map to `turn/start` on the current thread
- in-flight continuation can map to `turn/steer`
- `/stop` should map to `turn/interrupt`

The latest README also confirms that `turn/start` can override:

- model
- cwd
- approval policy
- sandbox policy
- reviewer
- other settings

Conclusion:

- the bridge should not invent a separate "run engine"
- it only needs to choose the right native turn operation

### 5.3 Model catalog and model switching

Relevant native methods:

- `model/list`
- `config/read`
- `config/value/write`
- `config/batchWrite`

Facts confirmed from the latest upstream:

- `model/list` returns native model catalog entries
- each model includes:
  - `id`
  - `displayName`
  - `description`
  - `hidden`
  - `supportedReasoningEfforts`
  - `defaultReasoningEffort`
  - `isDefault`
- config supports `model_reasoning_effort`
- thread start/resume responses also surface effective `reasoningEffort`

Conclusion:

- `/model` should read native model catalog and write native config
- reasoning effort should be treated as a native model/config capability, not bridge-owned truth

Primary local sources:

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\Model.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server\tests\suite\v2\model_list.rs`
- `D:\desktop\codex-upstream\codex-rs\app-server\tests\suite\v2\config_rpc.rs`
- `D:\desktop\codex-upstream\codex-rs\app-server\tests\suite\v2\thread_start.rs`

Official source:

- [GPT-5.3-Codex model docs](https://developers.openai.com/api/docs/models/gpt-5.3-codex)

### 5.4 Permission configuration

Relevant native methods:

- `config/read`
- `config/value/write`
- `config/batchWrite`

Facts confirmed from the latest upstream:

- native config owns approval policy, approvals reviewer, sandbox mode, and related settings
- thread start/resume responses surface effective:
  - `approvalPolicy`
  - `approvalsReviewer`
  - `sandbox`

Conclusion:

- `/permission` should write native config
- bridge permission presets are only UX presets mapping into native config

## 6. Native Output Surface We Must Ingest

The rewrite must assume that Codex emits more than just final text.

### 6.1 Thread and turn lifecycle notifications

Common lifecycle notifications include:

- `thread/started`
- `thread/status/changed`
- `thread/archived`
- `thread/unarchived`
- `thread/closed`
- `turn/started`
- `turn/completed`
- `thread/tokenUsage/updated`

Conclusion:

- the bridge must route these by native `threadId`
- even if some of them are hidden from users, the bridge still needs to consume them safely

### 6.2 Item stream

The latest README and protocol schema confirm the turn stream includes:

- `item/started`
- item-specific deltas
- `item/completed`

Current native `ThreadItem` variants include at least:

- `userMessage`
- `agentMessage`
- `plan`
- `reasoning`
- `commandExecution`
- `fileChange`
- `mcpToolCall`
- `dynamicToolCall`
- `collabAgentToolCall`
- `webSearch`
- `imageView`
- `imageGeneration`
- `enteredReviewMode`
- `exitedReviewMode`
- `contextCompaction`

Conclusion:

- the bridge must be prepared to ingest the full native item surface
- default product visibility may hide many of these, but ingestion cannot be selective

Primary local sources:

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadItem.ts`

### 6.3 Commentary vs tool calls vs final

For current product behavior, the useful display rule is:

- show commentary/progress
- hide tool call detail by default
- show final answer

But the native stream does not arrive pre-classified for our product.
So the bridge must do two steps:

1. ingest every native item and notification safely
2. apply visibility rules after classification

Conclusion:

- "hide tool calls" must mean "hide after parsing", not "do not handle"
- this is a direct guard against the hanging class of bugs we already saw

## 7. Native Interactive Request Surface

Even in a mostly full-access product, the bridge must know what kinds of server-initiated requests Codex can send.

The latest upstream README documents at least these request flows:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`
- `item/tool/requestUserInput`
- `mcpServer/elicitation/request`
- `item/tool/call` for dynamic tools
- `serverRequest/resolved` cleanup notification

### 7.1 Command approval

Native command approval requests carry:

- `threadId`
- `turnId`
- `itemId`
- possibly `approvalId`
- `reason`
- command display fields such as `command`, `cwd`, and parsed command actions

### 7.2 File-change approval

Native file-change approval requests carry:

- `threadId`
- `turnId`
- `itemId`
- optional `reason`
- sometimes unstable grant-root style fields

### 7.3 Permission approval

The native `request_permissions` tool uses:

- `item/permissions/requestApproval`

Current schema shows:

- request params include `threadId`, `turnId`, `itemId`, `reason`, and requested permission profile
- response includes granted permissions and scope

### 7.4 Tool request user input

Current schema shows:

- `item/tool/requestUserInput`
- params include `threadId`, `turnId`, `itemId`, and a list of questions

### 7.5 Dynamic tool calls

Current upstream still marks `dynamicTools` and `item/tool/call` as experimental.

Conclusion:

- the bridge must know all of these native request types exist
- even if the default product usually runs in full-access, unsupported interactive requests must never be silently left hanging
- every native request must end in one of:
  - projected and answered
  - explicitly auto-denied
  - explicitly rejected as unsupported

Primary local sources:

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\CommandExecutionRequestApprovalParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\FileChangeRequestApprovalParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\PermissionsRequestApprovalParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\PermissionsRequestApprovalResponse.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ToolRequestUserInputParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\DynamicToolCallParams.ts`

## 8. Native-First Conclusions For The Rewrite

Based on the latest upstream, these conclusions are now hard constraints.

### 8.1 Thread truth must remain native

Why:

- native APIs already support thread start, resume, fork, list, and read
- thread start/resume responses already include effective cwd, model, approval policy, sandbox, and reasoning effort

Rewrite implication:

- the bridge must not keep a separate authoritative thread model
- local state should only remember which native thread the current IM conversation is attached to

### 8.2 Model, permission, and reasoning truth must remain native

Why:

- native config and model APIs already expose these directly
- upstream tests confirm reasoning-effort persistence and override behavior

Rewrite implication:

- `/model`, `/permission`, and future reasoning-effort controls should be thin UX wrappers over native config and native catalog data

### 8.3 Thread browser may keep temporary UX state

Why:

- `/threads` is an IM UX page, not a native Codex concept

Rewrite implication:

- bridge-owned thread browser context is acceptable
- but the actual thread list data should come from native `thread/list`

### 8.4 Output handling must be total, display may be partial

Why:

- Codex emits many item and request types, and some will evolve over time

Rewrite implication:

- the bridge must safely consume all native outputs
- visibility rules should only decide what users see, not what the runtime handles

Important clarification:

- "consume all native outputs" does not mean "request every possible high-volume notification and synchronously process it on the socket read path"
- when Codex exposes native notification suppression such as `optOutNotificationMethods`, the bridge should use it to avoid receiving streams the default product does not need
- native opt-out is preferable to bridge-local wasteful ingestion when the product intentionally hides that class of output by default

### 8.5 Unsupported requests must fail explicitly

Why:

- server-initiated requests pause native execution until the client answers or rejects

Rewrite implication:

- silent drops are never acceptable
- unsupported native requests must produce a deliberate response path

## 9. Minimal Local State That Still Seems Necessary

Even under a thin-bridge design, some local state is still justified.

### 9.1 Conversation route

Minimum likely shape:

- `channel_id`
- `conversation_id`
- current native `thread_id` or none
- bootstrap or currently selected `cwd`

Purpose:

- route IM text into the right native thread
- know what cwd to use when `/new` creates a fresh thread

### 9.2 Thread browser context

Minimum likely shape:

- which conversation opened `/threads`
- current page
- current thread ids displayed on that page
- expiration

Purpose:

- support `/pick`, `/next`, `/prev`, `/exit`

### 9.3 IM reply metadata

Minimum likely shape:

- platform-specific reply anchor fields

Purpose:

- send async commentary/final messages back to the correct IM conversation in the expected platform style

### 9.4 Approval routing helper

If approval UX remains in product scope, the bridge still needs a small amount of transient routing data:

- native request id
- conversation route
- thread id
- turn id

Important limitation:

- this helper state exists only to reply to native requests
- it must not become a second request/approval engine

## 10. Risks And Edge Cases The Rewrite Must Respect

### 10.1 The native surface is broad and still evolving

The latest sync added or changed many protocol and app-server files.
That means:

- we should expect method and schema growth
- the bridge must be conservative about unsupported messages

### 10.2 Websocket use is a product risk if we depend on it

The local upstream docs still position stdio framing as the normal App Server shape and websocket as experimental.

If our deployment keeps websocket, we should:

- treat it as an explicit compatibility layer
- make reconnect and cleanup behavior very deliberate
- design for bounded-queue backpressure instead of assuming the bridge can always read fast enough
- keep transport reads decoupled from projection and observability work

### 10.3 Full-access does not mean approvals can be ignored forever

Even if the default product mode is full-access:

- config drift
- future defaults
- special tools
- hosted or restricted environments

may still surface native approval or user-input requests.

So:

- approval handling remains a real part of the product behavior
- it just is not on the hot path most of the time

### 10.4 Thread switching must reflect native cwd

The product requirement is:

- switching thread also switches effective cwd

That means:

- thread switch success text must use native response data
- do not derive cwd from stale local state if the native response already gives it

### 10.5 Output projection bugs are likely to show up as hangs

Because native requests can pause execution:

- parse failures
- unrecognized request types
- dropped request routing

may all look like "Codex got stuck".

This is a central rewrite lesson:

- the bridge must separate "ingest everything" from "show selectively"

### 10.6 Slow-consumer disconnects can look like mysterious pauses

The reproduced websocket failure showed a different hang shape from approval deadlocks:

- App Server disconnected the websocket client after outbound queue pressure
- the bridge retained stale `active_turn` state
- the next user message forced reconnect, rehydrate, failed steer, and fallback to a fresh turn

From the user's perspective, this looks like:

- Codex ran for a while
- then silently paused
- then "woke up" only after the next message

Rewrite implication:

- connection loss handling must be treated as part of conversation correctness
- stale turn cleanup after reconnect is mandatory
- observability and projection must not be allowed to make the bridge itself the slow consumer

## 11. Concrete Rewrite Checklist Derived From This Research

Before or during implementation, we should be able to answer yes to all of these:

- Do we have a stable `InboundMessage` and `OutboundMessage` contract for adapters?
- Do we route each IM conversation to exactly one current native thread?
- Does `/new` use native `thread/start` with the selected cwd?
- Does `/threads` use native `thread/list` rather than local snapshots as truth?
- Does thread switch confirmation show the actual native `cwd`?
- Do normal user messages map to native turn operations rather than bridge-owned run state?
- Does `/model` read native `model/list` and write native config?
- Does `/permission` write native config rather than bridge-owned permission state?
- Can we ingest all native item and lifecycle notifications even if we hide some of them?
- Do we use native notification opt-out for high-volume streams the default product does not need?
- Is the transport read path decoupled from slow logging and projection work?
- Do unsupported native requests fail explicitly instead of hanging?
- If websocket disconnects mid-turn, do we promptly reconcile stale local turn state?
- Do we keep only minimal local helper state for IM UX and routing?

## 12. Source Index

### Local `imcodex` sources

- `src\imcodex\models.py`
- `src\imcodex\channels\qq.py`
- [product-behavior-spec.md](product-behavior-spec.md)
- [system-constraints-spec.md](system-constraints-spec.md)
- [0001-native-thin-bridge.md](adr/0001-native-thin-bridge.md)

### Local `codex-upstream` sources

- `D:\desktop\codex-upstream\codex-rs\app-server\README.md`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadStartParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadListParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadStartResponse.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadResumeResponse.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\Model.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ThreadItem.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\PermissionsRequestApprovalParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\PermissionsRequestApprovalResponse.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server-protocol\schema\typescript\v2\ToolRequestUserInputParams.ts`
- `D:\desktop\codex-upstream\codex-rs\app-server\tests\suite\v2\model_list.rs`
- `D:\desktop\codex-upstream\codex-rs\app-server\tests\suite\v2\config_rpc.rs`
- `D:\desktop\codex-upstream\codex-rs\app-server\tests\suite\v2\thread_start.rs`

### Official OpenAI sources

- [Unlocking the Codex harness: how we built the App Server](https://openai.com/index/unlocking-the-codex-harness/)
- [GPT-5.3-Codex model docs](https://developers.openai.com/api/docs/models/gpt-5.3-codex)
