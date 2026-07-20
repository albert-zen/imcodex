# Product Behavior Spec

Status: Draft

## Goal

This document defines the accepted user-visible behavior of `imcodex`.

It is intentionally a product behavior spec, not an implementation spec.
It answers:

- what commands exist
- what the user should see
- what each command does
- what happens when Codex or the bridge cannot complete the request

It does not answer:

- how the bridge is internally modeled
- how many internal state objects exist
- how protocol adapters are implemented

## Product Model

The product exposes one active Codex thread per IM conversation.

For behavior purposes, the user should be able to think in terms of:

- current conversation
- current working directory
- current thread

The bridge is responsible for making that feel continuous inside one IM conversation.

## Channel Admission and Identity

Every remote IM adapter must use the platform's stable sender identifier for
admission. Display names are presentation only and must not authorize local
Codex access.

Product behavior:

- channel access restrictions are optional; empty lists or `*` accept messages
  within the scope already delivered by the platform
- `none`, used by itself in either list, explicitly accepts nobody while the
  channel remains connected
- concrete user and conversation IDs are independent restriction dimensions
- when both dimensions are active, `access_match=any` (the default) accepts a
  matching user or conversation, while `access_match=all` requires both
- group-capable adapters require an explicit bot mention by default
- topic/thread identifiers are part of the IM conversation identity so two
  platform topics do not silently share one native Codex thread
- rejected messages never reach bridge commands or the Codex execution path
- outbound projection rechecks the current admission policy using the stable
  sender ID retained as IM routing context; removing an identity revokes later
  delivery after restart/reconciliation as well as new inbound use
- admitted identities are full operators for this personal bridge, not
  low-privilege chat participants
- disabling a channel remains the only way to disconnect it; access policy is
  not a second enabled state

The Weixin iLink adapter is an experimental direct text-and-image transport.
QR login sets the scanning user's stable iLink ID as the default owner. Enterprise
WeCom and personal Weixin are different products and must not be represented as
one ambiguous channel.

## Native Authority

The product must use native Codex behavior as the source of truth for core agent operations.

This includes:

- thread creation
- thread listing
- thread switching
- model configuration
- permission configuration
- reasoning-effort configuration
- credits, usage, and rate-limit truth
- native request identity and approval flow

The bridge may keep only the minimum local state needed to support IM UX.

Examples of acceptable bridge-owned helper state:

- current conversation -> current native thread mapping
- current conversation working directory before or around thread creation
- temporary thread-browser page state for `/threads`
- IM-specific reply metadata

The bridge must not introduce a second authority for:

- thread truth
- model truth
- permission truth
- reasoning-effort truth
- credits, usage, or rate-limit truth
- request identity truth

The default operating configuration must align to native Codex settings:

- `approvalPolicy = never`
- `sandbox = danger-full-access`

## Default Visibility

The default bridge visibility profile is `standard`.

In `standard`:

- final answers are shown
- commentary/progress may be shown
- tool-call detail is hidden by default
- system noise is hidden by default

Users may later change visibility with `/view`, `/show`, and `/hide`, but this document focuses on the core command surface first.

## Natural-Language Messages

When the user sends a normal text message:

- if no working directory and no active thread exist, the bridge returns onboarding guidance
- otherwise the message is sent to the current Codex thread
- every normal user message is treated as a request to run Codex
- if a turn is already running, the bridge may continue that turn instead of starting a fresh one
- visible output is delivered asynchronously as commentary/progress and final result messages

This behavior spec does not require a separate immediate `accepted` message for every text input.

## Image Messages

Image input from QQ, Telegram, Feishu/Lark, Weixin, and the generic webhook
follows the same conversation, admission, and native-thread behavior as normal
text input. It does not introduce an image access mode, per-user media setup,
or a separate bridge-owned vision model.

Product behavior:

- private messages and webhook requests may contain only images or text plus
  images
- group images use the channel's existing mention/reply rule; admitted group
  members do not need to be registered individually
- static JPEG, PNG, and WebP are supported; animated images are rejected, with
  at most four images per message and a maximum downloaded size of 10 MiB and
  decoded size of 40 megapixels per image
- platform image order is preserved and message text is submitted alongside it
- for an image-only platform message, the native turn includes one neutral
  `[Image]` text item before the images so Codex App can render the user turn;
  this is presentation compatibility, not a generated caption or image analysis
- if no working directory and no active thread exist, the normal onboarding
  guidance is returned instead of starting an image turn
- if a turn is already running, the mixed input may steer that native turn under
  the same rules as a text message
- unsupported, malformed, oversized, unavailable, or unstaged images produce a
  concise user-visible error; they are not silently dropped
- a message over the image count or size limit is rejected rather than partially
  submitted

The bridge passes accepted images to native Codex as `localImage` inputs. The
native Codex model remains the authority for whether it can interpret the
image; imcodex does not select a second vision model or run a fallback OCR
pipeline. The bridge and App Server must share the staged-file namespace for
image input. Bridge-child stdio and the normal Unix-socket daemon are the only
accepted image topologies by transport alone. A native Windows
`ws://127.0.0.1:<port>` App Server is also accepted, whether launcher-selected
or explicitly configured, only after the project core manifest, listener owner,
live Codex command, and readiness probe are verified for the current connection
epoch. Other explicit TCP targets remain image-ineligible while text use stays
available; imcodex does not infer filesystem sharing from reachability or the
`localhost` spelling alone.

## Quoted Messages

When an IM platform includes a native quoted-message snapshot in an inbound
event, imcodex preserves that context in the same Codex user turn. The user
does not need to copy the original message or use a bridge command.

Product behavior:

- the native quote is presented to Codex as a clearly bounded quoted-message
  block followed by the user's current message
- quoted text is preserved, while quoted image, voice, video, and file items
  are represented by concise media labels and available platform transcripts
- when the platform reports a quote but omits its original snapshot, Codex is
  still told that the quoted content is unavailable rather than silently
  receiving only the new text
- QQ supports this behavior for native C2C and group quote events using the
  snapshot already delivered by QQ
- quote handling does not create a bridge-owned conversation history or fetch
  an unrelated platform history API; native Codex remains the conversation
  authority

Native Codex currently has no structured quote input type. The quoted block is
therefore a thin text translation inside the ordinary native user input, not a
second message or thread model.

## Command Surface

### `/help`

`/help` should be a compact grouped command map, not a command dictionary.
It should show user-facing product commands only.

It should use these groups:

Start:

- `/cwd <path>`
- `/new`

Threads:

- `/threads`
- `/history [turns]`
- `/fork`
- `/rename <name>`
- `/compact`

Run:

- `/status`
- `/goal [objective|pause|resume|clear]`
- `/stop`

Settings:

- `/model [model-id]`
- `/think [effort]`
- `/personality [style]`
- `/fast [on|off|status]`
- `/permission [mode]`

Account:

- `/credits [reset [number|credit-id]]`

Advanced:

- `/native help`

It should not expose app-server internal server requests such as `currentTime/read`.

It does not need to expose contextual thread-browser commands or advanced commands such as:

- `/pick`
- `/next`
- `/prev`
- `/exit`
- native escape-hatch commands

### `/cwd`

`/cwd` without arguments shows the current working directory.

`/cwd <path>`:

- validates that the directory exists
- sets the conversation's working directory
- uses that directory for the next new thread

`/cwd playground`:

- resolves a default playground directory
- creates it if needed
- sets it as the conversation working directory

If the path does not exist, the user gets an immediate error.

### `/new`

`/new` starts a new Codex thread in the current working directory.

Behavior:

- it requires a current working directory
- it creates a fresh thread
- it switches the current conversation to that thread
- it returns a status message containing the new thread id

If no working directory is set, the user gets an immediate error telling them to choose one first.

### `/threads`

`/threads` opens a browsable thread list for the current conversation.

Behavior:

- it queries Codex for available threads
- it follows native `thread/list` cursors to completion in batches and renders an
  exact local page count from that native result
- it may filter by a search term
- it may filter the current native result by an exact native `cwd` or `path`
  selected through `--project <name-or-number>`
- it may support `--page N`

The complete result is only short-lived thread-browser state. It must be
refreshed from native Codex when `/threads` is opened again and must not become a
durable thread or project index. Project choices are presentation labels derived
from exact native `cwd` or `path` values; they do not create a separate project
model. Numbered project choices remain bound to the active browser result, and
the inline project legend stays bounded so it cannot grow with the entire native
catalog.

The bridge should not maintain its own thread source allowlist. Native Codex
owns which thread sources are visible, including standalone app conversations
that are not nested under a user-selected project folder. The unfiltered
browser may place the current thread first, but otherwise preserves native
`updated_at` order. It must not group every thread matching the current `CWD`,
because that makes a fresh `/threads` look as if the previous project filter is
still active.

The list should clearly indicate:

- which thread is current, using one compact visual marker on that row
- each visible thread's label
- each visible thread's working-directory label, when known, rendered as a
  visually distinct bracketed badge rather than concatenated with the thread label

The compact browser should not repeat a field name on every row or expose native
load-state labels such as `notLoaded`; those details add noise without helping the
user choose a thread.

The rendered list should tell the user what to do next:

- `/pick <n>` to switch
- `/next` and `/prev` to paginate
- `/new` to start fresh
- `/exit` to close the browser

If thread listing fails, the user gets a friendly status message rather than raw upstream protocol output.

The working-directory label is derived only from native `cwd` or `path` metadata
that Codex returned for the thread, including last-known native metadata retained
across partial native updates. The bridge must not infer a separate project model
from unconfirmed or app-private fields.

### `/next`, `/prev`, `/pick <n> [--history [N]]`, `/exit`

These commands operate on the active thread browser opened by `/threads`.

Behavior:

- `/next` moves to the next page
- `/prev` moves to the previous page
- `/pick <n>` switches to the selected thread from the current page
- `/pick <n> --history` requests one recent turn after switching
- `/pick <n> --history N` requests between one and five recent turns
- `/exit` closes the active thread browser

When `/pick <n>` succeeds:

- the system must clearly tell the user that the thread has been switched
- the success message must identify the selected thread in a user-friendly way
- because thread switching also updates the effective working directory, the success message must show the new `CWD`
- the success message must show whether native Codex reports the selected thread as working or idle

If the selected thread is already running:

- the switch still succeeds
- a supplied `--history` option is ignored with an explicit explanation
- history that predates the switch is not replayed
- native messages produced while the switch is being established are buffered only until the switch notice is delivered, then projected in their original order
- later visible native messages continue to the newly bound IM conversation without waiting for another inbound IM message
- if the selected thread was started in Codex Desktop and is still running,
  its later commentary, requests, and terminal result follow the same live
  projection path after the switch

If the selected thread is idle and history was requested:

- the switch notice is delivered first
- the requested completed-turn history is delivered next
- if a new native turn starts while history is being read or delivered, its events wait behind that history and are then projected in order

The ordering buffer is bounded, transient presentation state. It must not
become a persisted copy of native thread or turn history, and it must not block
the App Server socket read path. Capacity pressure must preserve native final
output and approval/input requests rather than silently dropping them. When
notifications and native approval/input requests use separate dispatch lanes,
their handoff projection still follows App Server receive order. If delivery
of the switch notice or requested history fails, buffered native output remains
behind the durable cached response and is released only after that same inbound
IM message is retried successfully. While that ordering gate exists, it keeps a
transient replay copy of the immediate response even if the normal bounded
response cache is evicted; an expired-cache notice must never release live
output. After the immediate response succeeds, a failed buffered-output send is
retried with bounded backoff using the same projected message and delivery ID.
The backoff interval is capped but retries continue until delivery succeeds or
the service closes, so a transient outage cannot strand a gate after a fixed
attempt count. Native approval and question requests use the same projected
message for retries within their bounded native delivery timeout, then reject
explicitly if delivery never recovers. Each retry first verifies that the
native request is still pending, so an approval or answer completed after an
ambiguous platform send is not presented again as a stale request. The final
timeout path performs the same pending check before returning a delivery error
to Codex.

Terminal results use a separate durable delivery checkpoint. The checkpoint
records only that a bound native thread/turn still owes the current IM route a
terminal result; it is never consulted as native active-turn truth. Once the
result is projected, the exact outbound message and stable delivery ID remain
in an outbox until a channel sink accepts delivery. Rehydration recovers a
watched turn that completed while the bridge process was stopped, and a staged
message survives another restart without re-running the native turn.
After staging, native thread cleanup or conversation rebinding does not discard
or reroute that exact message; it remains owed to the IM route captured at
projection time.
Recovery remains degraded while any staged terminal message is still pending.
If a completed native turn contains no final text or usable buffered output,
the bridge sends an explicit empty-result notice instead of acknowledging a
blank channel no-op as successful delivery.

For the generic webhook, the immediate command response is always available in
the HTTP response. Live handoff requires `IMCODEX_OUTBOUND_URL`, because later
native messages occur after the inbound HTTP exchange. If no outbound callback
can route that generic channel, `/pick` and `/history` fail explicitly rather
than switching into an undeliverable live stream.

If no thread browser is active, `/next`, `/prev`, and `/pick` should return a user-facing error telling the user to run `/threads` first.

### `/history [N]`

`/history` shows one recent completed turn for the active native thread.
`/history N` shows between one and five recent completed turns. The older
`/thread history [N]` spelling remains a compatibility alias but is not the
primary documented command.

Behavior:

- it requires an active thread
- it is available only while native Codex reports the thread as idle
- while the thread is running, it returns a status explaining that history can be requested after the current turn completes
- it reads recent turns from native Codex using `thread/turns/list` or a native thread read that includes turns
- it renders each selected turn as ordinary Markdown text with a distinct turn heading, quoted user input, and structurally preserved final Codex output; it does not introduce a second message type or replay commentary, reasoning, tool calls, or raw protocol payloads
- if native history cannot be read, the user gets a friendly status instead of protocol noise
- if a new turn starts after an idle history read begins, live projection waits until the history response has been delivered so old and new output cannot interleave

### `/fork`, `/rename <name>`, `/compact`

These commands are thin wrappers over native thread operations.

Behavior:

- `/fork` requires an active thread, calls native `thread/fork`, and switches the current IM conversation to the forked native thread returned by Codex
- `/rename <name>` requires an active thread and calls native `thread/name/set`
- `/compact` requires an active thread and calls native `thread/compact/start`
- native failures are summarized in product language rather than raw protocol output
- `/archive` and `/unarchive` remain out of scope until archived thread browsing has a product design

### Agent thread tools

When IMCodex is the declared dynamic-tool host, every fresh native thread that
IMCodex starts through `thread/start` MUST receive model-callable tools for
listing and reading native threads, sending a message to a native thread, and
creating another thread. If a thread already has Desktop-registered tools,
IMCodex leaves that tool set intact. Supported thread-tool calls are handled
through native App Server APIs regardless of whether Desktop, CLI, or IMCodex
originally created the calling thread.

`create_thread` starts the child in the calling thread's native working
directory, starts its initial turn, and gives the child the same thread-tool
set. This recursive registration is required so an IMCodex-created child has
the same capabilities as its parent. The bridge does not create a project
catalog, worktree manager, pin model, handoff model, or remote-host registry to
support these tools.

Thread creation origin is not a capability or authorization boundary, so
IMCodex does not persist an ownership list for tool routing. Host selection is a
connection-topology decision: a private or declared IMCodex host resolves the
native mappings immediately, while an explicit shared endpoint leaves the
request to the client that registered the tools. If native child creation
succeeds but initial-turn startup cannot be confirmed, the failed tool result
still includes the created thread ID and tells the agent to inspect or message
it instead of blindly creating another child.

The current native protocol accepts `dynamicTools` on `thread/start`, but not
on `thread/resume`, `turn/start`, or `thread/fork`. Consequently IMCodex injects
its tool set when it creates a thread and preserves existing Desktop tools when
attaching, but cannot retrofit tools into an already-created thread that never
had them. It must not replace or fork the user's thread to simulate injection.

Native `thread/fork` does not currently accept dynamic-tool registration.
Therefore agent-facing `fork_thread`, rename, and archive tools are not included
in the IMCodex-created thread tool set. The user-facing `/fork` command retains
native fork semantics, but its result is not covered by this tool-injection
guarantee until the upstream protocol can register tools on a fork.

### `/status`

`/status` returns a compact overview of the current conversation state.

It should show:

- current cwd
- current thread
- current state such as idle or working
- App Server connection status, ownership, transport, safe endpoint, and connection epoch
- current model
- current reasoning effort
- current Fast mode state
- current permission mode
- current bridge visibility profile
- pending approval count when approvals are enabled in the product

If Codex cannot provide fresh thread status, the user should still get the local
App Server connection facts and a safe `Unavailable` thread state rather than
protocol noise.

### `/goal`

`/goal` without arguments shows the current native Codex goal for the active thread.

`/goal <objective>` sets or replaces the active thread goal.

`/goal pause`, `/goal resume`, and `/goal clear` update or clear the active thread goal.

Behavior:

- goal operations use native Codex `thread/goal/get`, `thread/goal/set`, and `thread/goal/clear`
- the bridge does not persist a second local goal source of truth
- `/goal <objective>` requires a current working directory if no native thread exists yet
- goal objectives must be non-empty and no longer than native Codex's 4,000-character limit
- if native Codex has the goals feature disabled or rejects the request, the user gets a friendly status response rather than raw protocol noise

### `/credits [reset [number|credit-id]]`

`/credits` shows the current ChatGPT credits, rate-limit status, earned
rate-limit resets, and usage summary reported by Codex. `/credits reset`
lets native Codex select the next reset; `/credits reset <number>` or
`/credits reset <credit-id>` consumes a specific returned reset.

Behavior:

- credits and rate-limit state are read from Codex with `account/rateLimits/read`
- usage summary is read from Codex with `account/usage/read`
- available earned resets are read from the native `rateLimitResetCredits` snapshot; the authoritative count may be greater than the returned detail rows
- reset details preserve native response order and display only a one-based number, reset/grant time, expiry time, and native scope title (falling back to `resetType`); opaque native IDs remain internal to selection and are not rendered
- numeric selection refetches the current native snapshot and resolves that number without persisting a local reset list; selection is limited to detail rows actually returned by Codex
- an opaque ID selector is passed directly as native `creditId`
- `/credits reset` calls native `account/rateLimitResetCredit/consume` with an idempotency key derived from the stable inbound IM request identity
- the reset command is itself the explicit user action; the bridge does not add a second confirmation or persist reset-credit state
- after every consume outcome, the bridge refetches native credits and limits instead of predicting the new quota locally
- native `reset` and `alreadyRedeemed` outcomes are treated as success; `nothingToReset` and `noCredit` are rendered as friendly results
- bare `/credits` is read-only and on-demand; users run it again when they want fresh data
- the bridge does not subscribe to `account/rateLimits/updated`, push quota updates, persist usage, or infer a local quota
- rate-limit window percentages are shown as remaining capacity rather than consumed usage
- rate-limit reset timestamps are rendered as local date/time using the user's or runtime environment's detected timezone, with UTC as the fallback if local timezone detection is unavailable
- if either rate limits or usage cannot be read, the response shows the successful partial data plus a friendly warning
- if Codex cannot provide the data, the user gets a friendly status response rather than protocol noise

### `/stop`

`/stop` interrupts the currently active turn for the conversation.

Behavior:

- if a turn is active, the bridge asks Codex to interrupt it
- if no turn is active, the command returns a friendly result instead of failing

### `/model`

`/model` without arguments shows the native model catalog and the current default model.

`/model <model-id>` sets the native default model.

`/model default` clears the native default model override.

Behavior:

- model changes are written to native Codex config
- the bridge does not invent a second long-term model authority

### `/think`

`/think` without arguments shows the current reasoning effort and the choices advertised by the selected native model.

`/think <effort>` sets the native default reasoning effort.

`/think default` clears the reasoning-effort override.

Behavior:

- reasoning-effort changes are written to native Codex config
- choices and descriptions come from `model/list[].supportedReasoningEfforts`
- the model's `defaultReasoningEffort` is identified in the command output
- if native model metadata is unavailable, the bridge may expose a compatibility list instead
- a non-default effort is rejected when the selected native model does not advertise it
- config changes reload the native user-config stack and apply as defaults to new threads;
  existing and resumed native threads retain their persisted thread settings
- already-loaded threads retain their native thread settings; the command output must not claim an immediate live-thread change
- the bridge does not maintain a second reasoning-effort truth

### `/personality`

`/personality` without arguments shows the current native personality configuration.

`/personality none`, `/personality friendly`, and `/personality pragmatic` select a native personality.

`/personality default` clears the override and returns personality selection to native Codex defaults.

Behavior:

- new, attached, resumed, and rehydrated threads omit thread-level personality overrides; native Codex applies any explicit global configuration
- personality changes reload native Codex config as a default for new threads; resumed threads
  retain their native thread setting
- already-loaded threads retain their native thread settings; changing them immediately would require an experimental native thread-settings operation
- the bridge does not force `friendly` or maintain a second personality truth

### `/fast`

`/fast` or `/fast status` shows the current Fast mode state.

`/fast on` enables Fast mode.

`/fast off` disables Fast mode.

Behavior:

- Fast mode changes are written to native Codex config
- enabling Fast mode writes the native Fast request tier, `service_tier = "priority"`
- disabling Fast mode explicitly selects standard routing with `service_tier = "default"`
- the bridge does not mutate `features.fast_mode`; that native feature gate controls whether
  clients expose service-tier selection, not the user's selected tier
- `priority` and the legacy config value `fast` are both displayed as Fast
- the configuration console follows the selected model's native service-tier catalog; it does not
  offer Fast for a model that does not advertise it, but still lets a user turn off a currently
  configured Fast tier
- when no explicit tier is configured, the selected model's native `defaultServiceTier` is effective;
  an explicit non-default tier is effective only when that model advertises the tier ID
- managed `configRequirements` defaults and feature requirements take priority and are not
  shadowed by editable bridge state
- the bridge does not maintain a second speed-mode truth

### `/permission`

`/permission` without arguments shows the current effective permission mode and the supported native permission profiles.

`/permission <mode>` selects the corresponding native permission profile where supported.

Supported product presets:

- `default`
- `read-only`
- `full-access`

Behavior:

- permission options are read from Codex with `permissionProfile/list` and `configRequirements/read`
- native profile selection is written to Codex config when profile support exists
- older Codex versions that do not expose native permission profiles may use the legacy approval/sandbox config fallback for the same product presets
- `full-access` maps to native full access behavior
- `full-access` means native `approvalPolicy = never` plus native `sandbox = danger-full-access`
- when no native permission choice exists at startup, the bridge initializes the native user config to `default_permissions = ":danger-full-access"` and `approval_policy = "never"`
- an existing native, legacy, or managed permission choice is preserved and remains authoritative
- managed defaults and requirements take precedence; if they disallow full access, startup preserves the managed behavior instead of bypassing it or failing the bridge
- bootstrap writes use the native user-config layer version so a concurrent user or administrator update cannot be silently overwritten
- permission config changes reload the native config stack as defaults for new threads rather than adding thread-level permission overrides
- existing and resumed threads retain their native permission settings
- the bridge does not maintain a second permission truth

### Approval Commands

The product supports approval handling only when native Codex requests it.

The default operating mode is native full-access:

- `approvalPolicy = never`
- `sandbox = danger-full-access`

In this default mode:

- no approval interaction should be needed
- users should be able to operate without seeing approval prompts

If native Codex does emit an approval request anyway, that is an exceptional path rather than normal product flow.
Typical causes include:

- the thread was started or resumed with the wrong native config
- the bridge resumed or routed the wrong thread
- native Codex behavior drifted from expected contract
- there is a bridge or upstream bug

If native Codex does emit an approval request, the bridge should surface it to the user as a request dialog/message associated with the native request id.

That approval surface should explain:

- `approve` allows the requested action
- `deny` rejects the requested action
- `cancel` cancels the current approval interaction without allowing it

It should also explain the batch behavior:

- `/approve` with no argument approves all currently pending requests in the conversation
- `/deny` with no argument denies all currently pending requests in the conversation
- `/cancel` with no argument cancels all currently pending requests in the conversation

It should explain targeted behavior:

- `/approve <prefix>` targets the request whose native request id matches that prefix
- `/deny <prefix>` targets the request whose native request id matches that prefix
- `/cancel <prefix>` targets the request whose native request id matches that prefix

Prefix matching should use a unique leading substring of the native request id.

If the user sends a normal non-command text message while approvals are pending:

- all currently pending approvals are canceled
- the new text is forwarded to Codex as the next user instruction

This matches the intended Codex CLI behavior.

## Output Classes

For command behavior, user-visible messages fall into three main classes:

- `status`
  For state changes, setup, switching, creation, and recovery feedback
- `command_result`
  For informational reads such as `/status`, `/threads`, `/model`, and `/permission`
- `error`
  For invalid input, blocked flows, missing context, or ambiguous selection

For asynchronous Codex output, the product may also emit:

- commentary/progress messages
- tool-call messages
- final turn result messages

## Codex Output Projection

During a normal Codex run, the bridge should be prepared to receive all native Codex output and classify it before deciding what to show.

The main product-visible categories are:

- tool calls
- commentary/progress
- final result

The default display behavior is:

- show commentary/progress
- hide tool-call detail
- show final result

The bridge should still ingest and understand native tool-call output even when it is hidden from the user by default.

In other words:

- all native Codex output should be received
- output should be normalized and classified
- visibility rules then decide what is rendered to the IM surface

## Error Handling

User-facing failures should be translated into product language.

Rules:

- invalid commands return immediate `error`
- missing required context returns immediate `error` or `status`, whichever is more helpful
- Codex/API failures should be summarized in friendly text
- raw transport, JSON-RPC, or stack-trace details must not be exposed to end users

## Deliberately Out Of Scope For This Spec

This document does not yet fully define:

- request-user-input UX
- native escape-hatch commands
- restart/reconnect semantics
- detailed event projection rules for every Codex item type

Those can be specified in follow-up behavior documents after the core command surface is locked.

## Acceptance Standard

A rewrite is acceptable only if a user can do the following end to end:

1. Read `/help` and discover the primary commands.
2. Set or inspect a working directory with `/cwd`.
3. Start a new thread with `/new`.
4. Browse threads with `/threads`.
5. Switch threads with `/pick`.
6. Inspect an idle thread's recent context with `/history`.
7. Inspect current state with `/status`.
8. Change the native model with `/model`.
9. Change the native permission preset with `/permission`.
10. Stop an active run with `/stop`.

If a new implementation satisfies these behaviors cleanly and predictably, it matches the current product intent even if the internal architecture is completely different.
