# IMCodex Native IM Interaction Model

This document defines the end-to-end interaction model for using Codex through
an IM channel.

It is not primarily a backend design document.
It is the product contract for how users and the bridge interact inside an
append-only message channel.

It should guide:

- command design
- message rendering
- approval UX
- state queries
- visibility settings
- recovery flows
- future implementation of the native-first bridge

## 1. Product Goal

The goal is:

- let users use Codex through an IM channel as natively as possible

without pretending that the IM channel is a desktop UI.

So the interaction model must preserve native Codex concepts:

- thread
- turn
- item
- approvals
- questions
- tool activity
- final result

while projecting them into an IM-compatible format:

- append-only messages
- text-first rendering
- command-driven control
- sparse optional file attachments

## 2. Core Interaction Principles

### 2.1 Natural language drives work

Normal user messages should mean:

- start or steer Codex work in the current thread and cwd

Examples:

- "帮我看一下这个报错"
- "继续，把测试也修掉"
- "先别改代码，只做 review"

### 2.2 Commands drive control

Any action that changes bridge state or responds to a native control point
should be a command.

This includes:

- selecting cwd
- creating or attaching threads
- changing permission profiles
- changing visibility profiles
- answering approvals
- answering structured questions
- checking status
- querying pending requests
- running diagnostics
- recovering from stale sessions

### 2.3 Channel history must remain readable

The channel is append-only, so every visible message should still make sense
when read later in sequence.

That means:

- no dependence on hidden widgets
- no dependence on editable cards
- no dependence on in-place patching
- no raw token spam
- no overly chatty routing noise

### 2.4 Codex should remain recognizable

The bridge should not invent an alien workflow.

Users should still be able to reason in native Codex terms:

- "I am in a thread"
- "this turn is running"
- "Codex is asking for approval"
- "Codex needs more input"
- "this result belongs to that turn"

## 3. User Mental Model

Users should only need to understand four things.

### 3.1 `cwd`

`cwd` is the working directory for this IM conversation.

It is the primary workspace concept.
Users do not need to learn bridge-internal `project id`.

### 3.2 Thread

A thread is the Codex conversation currently bound to the IM conversation.

It persists across turns and should be resumable after restart.

### 3.3 Turn

A turn is one unit of Codex work triggered by a user message.

A turn may:

- produce progress
- ask for approval
- ask for more input
- finish normally
- fail
- be interrupted

### 3.4 Ticket

A ticket is the IM-friendly handle for a pending native request.

Users interact with tickets through commands like:

- `/approve`
- `/deny`
- `/answer`

## 4. Conversation Modes

The IM surface should support two high-level message types.

### 4.1 Work messages

These are plain user messages.
They start or steer agent work.

Examples:

- "修一下这个模块的类型错误"
- "继续"
- "改成只读方案，不要写文件"

### 4.2 Control messages

These are explicit commands.
They inspect or modify the bridge/session state.

Examples:

- `/cwd D:\repo`
- `/status`
- `/threads`
- `/approve 3`

## 5. Command Surface

The command surface should be complete enough that all operational control can
be done inside the IM channel.

## 5.1 Workspace And Thread Commands

- `/cwd <path>`
  Set or switch the working directory for the conversation.
- `/status`
  Show cwd, active thread, active turn, current mode, and pending requests.
- `/threads`
  List native threads relevant to the current cwd or current conversation.
- `/thread attach <thread-id>`
  Bind this IM conversation to an existing native Codex thread.
- `/new`
  Start a new native thread under the current cwd.
- `/stop`
  Interrupt the active turn.
- `/recover`
  Recover from stale or invalid thread binding.

## 5.2 Approval And Input Commands

- `/approve <ticket...>`
  Accept one or more pending requests.
- `/approve-session <ticket...>`
  Accept one or more requests for the current session when native semantics
  support it.
- `/deny <ticket...>`
  Decline one or more requests.
- `/cancel <ticket...>`
  Cancel one or more requests without approving them.
- `/answer <ticket> key=value ...`
  Answer a structured input request.
- `/requests`
  List pending request tickets and their meanings.

## 5.3 Settings Commands

- `/permissions <profile>`
  Select a native Codex permission profile.
- `/view <profile>`
  Select the visibility profile for tool and progress output.
- `/show commentary`
  Show intermediate commentary-level model messages.
- `/hide commentary`
  Hide intermediate commentary-level model messages.
- `/show toolcalls`
  Show tool-call-level messages.
- `/hide toolcalls`
  Hide tool-call-level messages.
- `/model <name>`
  Override the working model when supported.
- `/mode <name>`
  Optional future command for collaboration or review mode selection.

## 5.3.1 Native permission profile requirements

Permission profile is a native Codex concept and should remain modeled that way.

For the IM surface, the required user-facing profiles are:

- `/permissions autonomous`
  Autonomous action mode. Codex should operate without asking the user for
  approval during normal work.
- `/permissions review`
  Default review mode. Codex should return to requiring manual approval where
  native policy requires it.

Design rule:

- the bridge should map these commands onto native Codex permission settings
  rather than inventing a separate bridge-only permission system

Recommended confirmation messages:

- `Permission profile set to autonomous.`
- `Permission profile set to review.`

## 5.4 Diagnostics Commands

- `/doctor`
  Show runtime diagnostics.
- `/thread read`
  Show current native thread metadata and identity.
- `/help`
  Show concise command help oriented around IM usage.

## 6. Outbound Message Types

The bridge should emit a small number of stable IM-visible message classes.

## 6.1 Immediate messages

These appear right after the inbound message is processed.

- `accepted`
  Normal natural-language request was received and work has started.
- `status`
  Command changed state or confirmed setup.
- `command_result`
  Command returned information.
- `error`
  Request was invalid, blocked, or failed immediately.

## 6.2 Asynchronous lifecycle messages

- `turn_progress`
  Meaningful intermediate progress.
- `approval_request`
  Codex needs approval.
- `question_request`
  Codex needs structured user input.
- `turn_result`
  Terminal result for the turn.

## 6.3 Optional future informational messages

If needed later, these can still be mapped into the existing classes or added
carefully:

- `turn_plan`
- `turn_diff`
- `diagnostic_notice`

But the default should be to keep the visible taxonomy compact.

## 7. Message Rendering Rules

The same underlying event should be rendered differently depending on its role.

### 7.1 Accepted message

Keep it short.

Good:

- "Working on it."
- "Continuing in the current thread."

Bad:

- verbose routing details
- raw ids unless needed

### 7.2 Progress message

A progress message should only be sent when it helps the user orient.

Good candidates:

- completed intermediate agent explanation
- plan update
- meaningful tool activity summary
- review-mode progress

Bad candidates:

- every token delta
- every low-level event
- repetitive "still working" chatter

### 7.3 Approval request

Approval requests should be explicit, scoped, and commandable.

Recommended format:

- ticket id
- what Codex wants to do
- cwd or target where useful
- any special risk context such as network access
- exactly which commands the user may send next

Approval prompts should also support batch action when multiple tickets are
open.

Recommended examples:

- `/approve 3`
- `/approve 3 4 5`
- `/approve-session 3 4 5`
- `/deny 8 9`

### 7.4 Question request

Question requests should be rendered as:

- ticket id
- question ids and human-readable prompts
- answer syntax example

### 7.5 Final result

Final result should prioritize:

1. the useful answer
2. terminal status if not successful
3. summarized command/file context only when needed

## 8. Visibility Profiles

Because IM channels have limited bandwidth for attention, tool visibility should
be profile-based.

However, three item layers should have explicit, user-controlled semantics
regardless of the broader profile:

1. final reply
2. commentary-level model messages
3. tool-call messages

These are the hard requirements for the IM surface.

### 8.0 Item-layer display rules

- final reply
  always shown and cannot be disabled
- commentary model messages
  shown by default, but the user may hide them
- tool-call messages
  hidden by default, but the user may show them

This means visibility configuration must support at least these commandable
switches:

- `/show commentary`
- `/hide commentary`
- `/show toolcalls`
- `/hide toolcalls`

Equivalent profile-based commands are also acceptable as long as the semantics
stay clear and command-driven.

## 8.1 `minimal`

Show:

- accepted
- approvals
- questions
- final result
- only the most important progress
- commentary hidden
- tool calls hidden

Best for:

- busy shared chats
- mobile-first usage

## 8.2 `standard`

Show:

- accepted
- approvals
- questions
- meaningful progress
- selected command/file summaries
- final result
- commentary shown
- tool calls hidden

Best for:

- default one-on-one usage

## 8.3 `verbose`

Show:

- all standard output
- plan updates
- more tool activity
- more diagnostics
- more recovery and state details
- commentary shown
- tool calls shown

Best for:

- debugging
- power users
- bridge development

## 9. Approval Interaction Model

Approvals should feel native to Codex but usable in plain chat.

## 9.1 Approval flow

1. Codex starts an item.
2. Codex requests approval.
3. Bridge creates an IM ticket.
4. User responds with an approval command.
5. Bridge forwards the native decision.
6. Bridge closes the ticket when native lifecycle confirms resolution.

Batch approval should be supported when multiple tickets are pending.

Example:

1. Codex creates tickets `1`, `2`, and `3`.
2. User sends `/approve 1 2 3`.
3. Bridge forwards approval for all three native requests.
4. Each ticket closes when its native request resolves.

## 9.2 Approval prompt requirements

Each approval prompt should include:

- ticket id
- request type
- short human summary
- exact follow-up commands

Where available, also include:

- command preview
- cwd
- file summary
- network target

## 9.3 Approval command examples

- `/approve 12`
- `/approve 12 13 14`
- `/approve-session 12`
- `/approve-session 12 13 14`
- `/deny 12`
- `/deny 12 13`
- `/cancel 12`
- `/cancel 12 13`

If one ticket in a batch fails to resolve, the bridge should report per-ticket
outcome instead of failing the whole batch opaquely.

## 10. Structured Input Interaction Model

Structured questions should remain machine-usable and human-readable.

## 10.1 Question flow

1. Codex asks for user input.
2. Bridge renders the questions in readable text.
3. User answers through `/answer`.
4. Bridge forwards structured answers.
5. Ticket closes when the native request resolves.

## 10.2 Answer syntax

Recommended syntax:

- `/answer 7 branch=main`
- `/answer 7 test_scope=unit,integration`

If multiple answers are required, the bridge should keep the syntax consistent
and predictable.

## 11. State Query Model

The user must be able to inspect state through the channel without hidden UI.

## 11.1 `/status`

Should show:

- current cwd
- current thread label
- current thread id
- current turn id and status
- permission profile
- visibility profile
- commentary visibility
- tool-call visibility
- pending ticket count

## 11.2 `/threads`

Should show:

- readable thread label
- thread id
- cwd if relevant
- current marker
- optional freshness or status hint

## 11.3 `/requests`

Should show:

- open tickets
- request type
- short summary
- which turn/thread they belong to if useful

## 11.4 `/doctor`

Should show:

- codex executable path
- app-server endpoint
- bridge endpoint
- pid
- data dir
- current mode/profile summary

## 12. Recovery Interaction Model

Recovery must be explicit and chat-friendly.

## 12.1 Stale thread recovery

When a thread cannot be resumed:

- do not silently swap in a new thread
- show a recoverable status message
- let the user choose a next step by command

Examples:

- `/recover`
- `/new`
- `/thread attach <thread-id>`

## 12.2 Interrupt behavior

When the user sends `/stop`:

- the bridge requests interruption
- the channel later receives a terminal interrupted result or explicit stopped
  status
- stale late output from the interrupted turn should be suppressed

## 13. File Attachment Policy

The IM channel can carry only limited files, so attachments should be sparse.

Attach a file only when:

- the content is too large for readable inline text
- structure matters, such as a patch, log, or machine-readable artifact
- the user explicitly asked for a file

Default behavior:

- summarize in text first
- attach only as a supplement

## 14. Example Session

### 14.1 Setup

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
/permissions interactive_session
```

Bridge:

```text
Permission profile set to interactive_session.
```

### 14.2 Work turn

User:

```text
帮我检查一下 app-server 的线程恢复逻辑
```

Bridge:

```text
Working on it.
```

Bridge later:

```text
Codex is tracing the current thread binding and resume path.
```

Bridge later:

```text
[ticket 3] Approval needed.
Run git diff to inspect local changes.
Use /approve 3, /approve-session 3, /deny 3, or /cancel 3
```

User:

```text
/approve 3
```

Bridge:

```text
Recorded accept for 3.
```

Bridge later:

```text
The resume path currently retries by clearing the active thread and starting a
new one, which risks silently replacing stale native thread identity.
```

## 15. Success Criteria

The interaction model is successful if:

- users can do all important control actions from the IM channel
- natural language and commands have clearly separated roles
- channel history remains readable after long sessions
- approval and question flows feel explicit rather than magical
- native Codex concepts remain visible and understandable
- the bridge feels like Codex in chat, not a custom bot with hidden state
