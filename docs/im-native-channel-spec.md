# IMCodex Native Channel Specification

This document is the authoritative specification for using Codex through an IM
channel in `imcodex`.

It defines:

- user-facing concepts
- command grammar
- message classes
- lifecycle rules
- permission switching
- approval semantics
- visibility semantics
- recovery behavior
- rendering requirements

This is the product and interaction spec for the IM surface.
Implementation should conform to this document.

## 1. Scope

This spec applies to any append-only IM channel integration of `imcodex`,
including webhook-backed chat surfaces and QQ-style channel transports.

This spec assumes:

- the UI is append-only from the user point of view
- interaction is primarily text
- file attachments exist but are limited
- all control actions must be invocable by command messages

This spec does not assume:

- rich cards
- in-place message editing
- buttons
- hidden local UI state

## 2. Design Goal

The goal is to let users use Codex through IM as natively as possible while
respecting IM channel constraints.

The bridge must preserve native Codex concepts:

- `cwd`
- thread
- turn
- item
- approval
- question
- final result

But it must project them into an IM-native format:

- append-only messages
- command-driven control
- text-first rendering
- sparse attachment use

## 3. Normative Language

The keywords `MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT`, and `MAY` are used in
their normal specification sense.

## 4. User-Facing Concepts

Users are expected to understand only these concepts.

### 4.1 `cwd`

`cwd` is the working directory bound to the IM conversation.

Rules:

- `cwd` is the primary workspace concept
- user-facing output MUST prefer `cwd` wording
- internal `project id` concepts MUST NOT be required for normal operation

### 4.2 Thread

A thread is the native Codex conversation currently bound to the IM
conversation.

Rules:

- one IM conversation SHOULD have one primary active native thread at a time
- thread identity MUST remain native Codex thread identity
- thread binding SHOULD survive bridge restart when possible

### 4.3 Turn

A turn is one unit of Codex work initiated by a user work message.

Rules:

- a turn MAY emit progress
- a turn MAY request approval
- a turn MAY request structured user input
- a turn MUST end in exactly one terminal state:
  - completed
  - interrupted
  - failed

### 4.4 Ticket

A ticket is the IM-friendly handle for a pending native App Server request.

Rules:

- each IM-visible pending request MUST have a ticket id
- ticket ids are conversation-local handles
- native request ids MUST remain internally mapped

## 5. Message Classes

The IM surface exposes two broad categories of user input and a fixed set of
bridge output classes.

## 5.1 User Input Categories

### 5.1.1 Work Message

A work message is ordinary natural language.

Semantics:

- starts or steers Codex work in the current thread and cwd

Examples:

- `帮我检查一下这个报错`
- `继续，把剩下的测试修掉`
- `只做 review，不要改文件`

### 5.1.2 Control Message

A control message is an explicit command.

Semantics:

- queries or modifies bridge/session behavior
- responds to pending native requests

Examples:

- `/cwd D:\repo`
- `/status`
- `/approve 3 4 5`

## 5.2 Bridge Output Classes

The bridge MUST use these visible message classes.

### 5.2.1 Immediate classes

- `accepted`
  normal work message accepted and work started
- `status`
  command changed state successfully
- `command_result`
  command returned information
- `error`
  invalid, blocked, or immediately failed request

### 5.2.2 Asynchronous classes

- `turn_progress`
  meaningful intermediate progress
- `approval_request`
  Codex needs approval
- `question_request`
  Codex needs structured user input
- `turn_result`
  terminal output for a turn

### 5.2.3 Output class stability

Rules:

- implementations SHOULD keep this output taxonomy stable
- richer future event types SHOULD be projected into this taxonomy when possible
- new visible classes SHOULD be added only when existing classes cannot express
  the semantics cleanly

## 6. Control Plane Rules

All non-freeform control actions MUST be command-driven.

This includes:

- selecting cwd
- selecting or creating thread binding
- switching permission mode
- switching item visibility
- approval and denial
- answering structured questions
- status inspection
- diagnostics
- recovery

The bridge MUST NOT require hidden UI controls for any essential operation.

## 7. Command Surface

The following commands define the IM control plane.

## 7.1 Workspace And Thread Commands

### `/cwd <path>`

Purpose:

- bind the current IM conversation to a working directory

Behavior:

- resolves the path
- validates that the directory exists
- sets current cwd
- does not itself start a turn

Success output:

- `status`

Failure output:

- `error`

### `/status`

Purpose:

- show current conversation/session state

Behavior:

- read-only

Output:

- `command_result`

### `/threads`

Purpose:

- list native Codex threads relevant to the current context

Behavior:

- SHOULD prefer native thread metadata over bridge-only historical state

Output:

- `command_result`

### `/thread attach <thread-id>`

Purpose:

- bind the current IM conversation to an existing native thread

Behavior:

- validates or resumes native thread identity
- updates current binding

Output:

- `status` on success
- `error` on failure

### `/thread read`

Purpose:

- inspect current native thread metadata

Output:

- `command_result`

### `/new`

Purpose:

- start a new native thread under the current cwd

Behavior:

- clears current thread binding for the conversation
- creates a new native thread

Output:

- `status`

### `/stop`

Purpose:

- interrupt the active turn

Behavior:

- requests native turn interruption
- does not guarantee immediate visual stop

Output:

- `command_result`

### `/recover`

Purpose:

- recover from stale or invalid thread binding

Behavior:

- implementation-defined recovery helper
- MUST NOT silently destroy native thread identity

Output:

- `status` or `command_result`

## 7.2 Approval And Input Commands

### `/approve <ticket...>`

Purpose:

- accept one or more pending requests

Batch semantics:

- one or more ticket ids MUST be accepted
- each ticket MUST be processed independently

Examples:

- `/approve 1`
- `/approve 1 2 3`

### `/approve-session <ticket...>`

Purpose:

- accept one or more pending requests using native session-scoped approval when
  supported

Examples:

- `/approve-session 2`
- `/approve-session 2 3 4`

### `/deny <ticket...>`

Purpose:

- decline one or more pending requests

### `/cancel <ticket...>`

Purpose:

- cancel one or more pending requests without approving them

### `/answer <ticket> key=value ...`

Purpose:

- submit structured answers for a pending question request

Examples:

- `/answer 7 branch=main`
- `/answer 7 test_scope=unit,integration`

### `/requests`

Purpose:

- list currently pending tickets for the conversation

## 7.3 Settings Commands

### `/permissions autonomous`

Purpose:

- switch to native autonomous action mode

Semantics:

- Codex SHOULD operate without asking the user during normal work
- mapping MUST be expressed through native Codex permission configuration

Output:

- `status`

### `/permissions review`

Purpose:

- switch back to native manual review mode

Semantics:

- Codex SHOULD require manual approval where native policy requires it

Output:

- `status`

### `/view minimal`

Purpose:

- set compact visibility profile

### `/view standard`

Purpose:

- set default visibility profile

### `/view verbose`

Purpose:

- set expanded visibility profile

### `/show commentary`

Purpose:

- show commentary-level intermediate model messages

### `/hide commentary`

Purpose:

- hide commentary-level intermediate model messages

### `/show toolcalls`

Purpose:

- show tool-call-level messages

### `/hide toolcalls`

Purpose:

- hide tool-call-level messages

### `/model <name>`

Purpose:

- set or override model where supported

### `/help`

Purpose:

- show concise command help

## 7.4 Diagnostics Commands

### `/doctor`

Purpose:

- show runtime diagnostics for the bridge environment

Output:

- `command_result`

## 8. Command Grammar

Commands are line-based and begin with `/`.

General grammar:

```text
command         = "/" name *(SP arg)
name            = 1*(ALPHA / "-" )
arg             = 1*(VCHAR / escaped-space)
ticket-list     = ticket *(SP ticket)
ticket          = 1*DIGIT
key-value       = key "=" value
key             = 1*(ALPHA / DIGIT / "_" / "-")
value           = 1*(VCHAR)
```

Practical parsing rules:

- quoted paths MAY be supported
- repeated spaces SHOULD be tolerated
- batch ticket commands MUST accept one or more whitespace-separated ticket ids
- `/answer` MUST parse `key=value` pairs deterministically

## 9. Permission Model

Permission mode is a native Codex concept.

The bridge MUST expose permission switching as a thin user-facing mapping over
native Codex policy.

### 9.1 Required user-facing modes

- `autonomous`
- `review`

### 9.2 Required behavior

- permission mode MUST be changeable by command
- the current permission mode SHOULD appear in `/status`
- the bridge MUST NOT invent a second unrelated permission system

### 9.3 Environment override rule

Environment variables MAY define a default startup permission mode.
User-issued permission commands MUST override the current conversation mode for
the active session unless explicitly disallowed by deployment policy.

## 10. Item Visibility Model

The IM surface must support separate visibility control for three item layers.

### 10.1 Final reply

Rules:

- final reply MUST always be shown
- final reply MUST NOT be hideable by visibility commands

### 10.2 Commentary model messages

Definition:

- intermediate model commentary intended for user consumption but not the final
  answer

Rules:

- commentary MUST be shown by default
- commentary MUST be hideable via command

### 10.3 Tool-call messages

Definition:

- messages representing tool invocation activity, such as commands, file
  changes, searches, or related tool-level summaries

Rules:

- tool-call messages MUST be hidden by default
- tool-call messages MUST be showable via command

## 10.4 Visibility Profiles

Profiles are convenience presets layered on top of item visibility controls.

### `minimal`

Defaults:

- final reply shown
- commentary hidden
- tool calls hidden

### `standard`

Defaults:

- final reply shown
- commentary shown
- tool calls hidden

### `verbose`

Defaults:

- final reply shown
- commentary shown
- tool calls shown

### 10.5 Interaction between profiles and toggles

Rules:

- `/view <profile>` MAY reset commentary/tool-call toggles to profile defaults
- `/show` and `/hide` commands MUST override current effective visibility after
  they are issued
- `/status` SHOULD report both current profile and effective commentary/tool-call
  visibility

## 11. Message Rendering Rules

## 11.1 Accepted

Rules:

- MUST be short
- MUST confirm work began
- SHOULD avoid raw routing details

Example:

```text
Working on it.
```

## 11.2 Status

Rules:

- MUST confirm a state change clearly
- SHOULD use user-facing vocabulary such as `cwd`, `thread`, `permissions`

## 11.3 Command result

Rules:

- MUST be scannable in plain text
- SHOULD group related state together

## 11.4 Progress

Rules:

- MUST be meaningful, not token-level spam
- SHOULD reflect commentary, plan, or selected tool progress based on visibility
- MUST NOT flood the channel with low-value repetition

## 11.5 Approval request

Approval requests MUST include:

- ticket id
- short summary
- exact next commands

When available, they SHOULD include:

- command preview
- cwd
- file target
- network target

Example:

```text
[ticket 3] Approval needed.
Run git diff to inspect local changes.
Use /approve 3, /approve-session 3, /deny 3, or /cancel 3
```

## 11.6 Question request

Question requests MUST include:

- ticket id
- question ids
- human-readable prompts
- answer syntax example

## 11.7 Turn result

Rules:

- MUST contain the final answer when successful
- MUST surface failure or interruption when not successful
- MAY append summarized command/file context only when useful

## 12. Lifecycle Rules

## 12.1 Work message lifecycle

For a normal work message:

1. bridge returns immediate `accepted`
2. bridge MAY emit `turn_progress`
3. bridge MAY emit `approval_request` or `question_request`
4. bridge MUST emit one terminal `turn_result`

## 12.2 Early final-answer rule

If Codex emits a final-answer item before terminal turn completion:

- the bridge MAY surface useful final text early
- terminal turn state still governs the authoritative outcome
- a later failure or interruption MUST override optimistic success rendering

## 12.3 Steering rule

If a user sends a new work message while a turn is in progress:

- the bridge SHOULD try to steer the active turn when native semantics allow
- if steer fails because the turn is not fully ready yet, the bridge MAY retry
  briefly
- stale output from superseded turns MUST be suppressed

## 13. Approval Semantics

## 13.1 Single approval

For one ticket:

1. user sends approval command
2. bridge forwards native decision
3. ticket remains pending until native request lifecycle confirms resolution

## 13.2 Batch approval

For multiple tickets:

1. user sends one command with multiple ticket ids
2. bridge processes each ticket independently
3. bridge reports per-ticket success or failure if outcomes differ

Example:

```text
/approve 1 2 3
```

Required behavior:

- batch approval MUST be supported
- one failing ticket MUST NOT silently invalidate successful ones
- user feedback SHOULD make per-ticket result visible when mixed

## 13.3 Ticket closure

Rules:

- a ticket SHOULD close when native lifecycle confirms resolution
- local send success alone SHOULD NOT be treated as authoritative closure

## 14. Structured Input Semantics

Rules:

- each question request MUST produce a ticket
- `/answer` MUST target exactly one ticket
- answers MUST remain machine-parseable
- ticket closes when native request resolves

## 15. Status Semantics

## 15.1 `/status` required fields

`/status` SHOULD show:

- current cwd
- current thread label
- current thread id
- current turn id
- current turn status
- current permission mode
- current visibility profile
- commentary visibility
- tool-call visibility
- pending ticket count

## 15.2 `/threads` required fields

`/threads` SHOULD show:

- readable thread label
- thread id
- cwd where relevant
- current active marker

## 15.3 `/requests` required fields

`/requests` SHOULD show:

- ticket id
- request type
- short summary

## 15.4 `/doctor` required fields

`/doctor` SHOULD show:

- codex executable path
- app-server endpoint or transport
- bridge endpoint
- process id
- data directory
- current permission mode
- current visibility mode

## 16. Recovery Semantics

## 16.1 Stale thread

If a thread cannot be resumed or validated:

- the bridge MUST NOT silently replace it with a new thread
- the bridge MUST surface a recoverable message
- the user MUST be able to choose the next step by command

Valid next steps include:

- `/recover`
- `/new`
- `/thread attach <thread-id>`

## 16.2 Interrupt

If the user sends `/stop`:

- native interruption SHOULD be requested
- eventual terminal state MUST be surfaced
- stale late-arriving output from the interrupted turn MUST be suppressed

## 17. File Attachment Policy

Files are supplemental, not primary.

Rules:

- the bridge SHOULD default to text summaries
- file attachment SHOULD be used only when inline text would be too large or too
  lossy
- file attachment MAY be used when the user explicitly asks for a file
- if a file is attached, the bridge SHOULD also describe it in text

## 18. Error Handling Rules

## 18.1 Command errors

Invalid commands MUST return `error` with usage guidance.

## 18.2 Mixed batch outcomes

If a batch approval command has mixed outcomes:

- bridge SHOULD return a clear per-ticket summary
- bridge MUST NOT hide partial success

## 18.3 Unknown native event

Rules:

- bridge MUST NOT crash
- bridge MAY log the event
- bridge MAY surface it only in verbose/diagnostic contexts

## 19. Example Session

### 19.1 Setup

User:

```text
/cwd D:\desktop\imcodex
```

Bridge:

```text
Working directory set to D:\desktop\imcodex.
```

User:

```text
/permissions autonomous
```

Bridge:

```text
Permission profile set to autonomous.
```

User:

```text
/hide commentary
```

Bridge:

```text
Commentary messages hidden.
```

### 19.2 Batch approval

Bridge:

```text
[ticket 1] Approval needed.
Inspect repository diff.
Use /approve 1, /approve-session 1, /deny 1, or /cancel 1
```

Bridge:

```text
[ticket 2] Approval needed.
Run unit tests.
Use /approve 2, /approve-session 2, /deny 2, or /cancel 2
```

Bridge:

```text
[ticket 3] Approval needed.
Open deployment config.
Use /approve 3, /approve-session 3, /deny 3, or /cancel 3
```

User:

```text
/approve 1 2 3
```

Bridge:

```text
Approved tickets 1, 2, and 3.
```

### 19.3 Work result

Bridge later:

```text
The current resume flow still risks silently replacing a stale native thread
with a new thread. The safer behavior is to surface a recoverable stale-thread
state and let the user choose /recover, /new, or /thread attach.
```

## 20. Acceptance Criteria

This spec is satisfied when:

- all essential control actions are available through commands
- permission mode switching uses native Codex semantics
- batch approval works for multiple tickets
- final reply is always shown
- commentary visibility is command-toggleable
- tool-call visibility is command-toggleable
- channel history remains readable after long sessions
- recovery is explicit rather than silent
