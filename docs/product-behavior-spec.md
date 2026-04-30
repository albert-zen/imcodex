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

## Native Authority

The product must use native Codex behavior as the source of truth for core agent operations.

This includes:

- thread creation
- thread listing
- thread switching
- model configuration
- permission configuration
- reasoning-effort configuration
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

## Command Surface

### `/help`

`/help` should show the compact top-level product commands, not every advanced or recovery-oriented command.

It should prominently include:

- `/cwd <path>`
- `/cwd playground`
- `/threads`
- `/new`
- `/status`
- `/stop`
- `/model [model-id]`
- `/think [effort]`
- `/fast [on|off|status]`
- `/permission [mode]`

It should include short examples for `/model`, `/think`, and `/permission`.

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
- it renders a paged list
- it may filter by a search term
- it may support `--page N`
- it may support `--all`

The list should clearly indicate:

- which thread is current
- each visible thread's label
- enough state to help the user choose

The rendered list should tell the user what to do next:

- `/pick <n>` to switch
- `/next` and `/prev` to paginate
- `/new` to start fresh
- `/exit` to close the browser

If thread listing fails, the user gets a friendly status message rather than raw upstream protocol output.

### `/next`, `/prev`, `/pick <n>`, `/exit`

These commands operate on the active thread browser opened by `/threads`.

Behavior:

- `/next` moves to the next page
- `/prev` moves to the previous page
- `/pick <n>` switches to the selected thread from the current page
- `/exit` closes the active thread browser

When `/pick <n>` succeeds:

- the system must clearly tell the user that the thread has been switched
- the success message must identify the selected thread in a user-friendly way
- because thread switching also updates the effective working directory, the success message must show the new `CWD`

If no thread browser is active, `/next`, `/prev`, and `/pick` should return a user-facing error telling the user to run `/threads` first.

### `/status`

`/status` returns a compact overview of the current conversation state.

It should show:

- current cwd
- current thread
- current state such as idle or working
- current model
- current reasoning effort
- current Fast mode state
- current permission mode
- current bridge visibility profile
- pending approval count when approvals are enabled in the product

If Codex cannot provide fresh status, the user should still get a safe, friendly status response rather than protocol noise.

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

`/think` without arguments shows the current native reasoning effort and supported command choices.

`/think <effort>` sets the native default reasoning effort.

`/think default` clears the native reasoning-effort override.

Supported product choices:

- `minimal`
- `low`
- `medium`
- `high`
- `xhigh`
- `default`

Behavior:

- reasoning-effort changes are written to native Codex config
- the bridge does not maintain a second reasoning-effort truth
- model-specific support and clamping remain native Codex behavior

### `/fast`

`/fast` or `/fast status` shows the current native Fast mode state.

`/fast on` enables native Codex Fast mode.

`/fast off` disables native Codex Fast mode.

Behavior:

- Fast mode changes are written to native Codex config
- enabling Fast mode writes `service_tier = "fast"` and `features.fast_mode = true`
- disabling Fast mode writes `service_tier = "standard"` and `features.fast_mode = false`
- the bridge does not maintain a second speed-mode truth

### `/permission`

`/permission` without arguments shows the current effective permission mode and the supported presets.

`/permission <mode>` writes the corresponding native permission settings.

Supported product presets:

- `default`
- `read-only`
- `full-access`

Behavior:

- permission changes are written to native Codex config
- `full-access` maps to native full access behavior
- `full-access` means native `approvalPolicy = never` plus native `sandbox = danger-full-access`
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
6. Inspect current state with `/status`.
7. Change the native model with `/model`.
8. Change the native permission preset with `/permission`.
9. Stop an active run with `/stop`.

If a new implementation satisfies these behaviors cleanly and predictably, it matches the current product intent even if the internal architecture is completely different.
