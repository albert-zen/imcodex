# IMCodex Issue Notes

This document records the main classes of issues we hit while making the
`imcodex` bridge usable. The point is to separate:

1. Codex-native behavior and protocol quirks
2. Windows-specific runtime problems
3. Channel and IM-platform integration problems

It is intentionally practical. Each item focuses on what we observed, why it
matters, and what we currently do about it.

## 1. Codex problems

These are issues or quirks that come from Codex itself, the `app-server`
protocol, or Codex session semantics rather than from our bridge code.

### 1.1 `turn/start` accepted does not always mean the turn is fully steerable yet

- Observed behavior:
  `turn/start` can return a result before the server has fully marked the turn
  as active for `turn/steer`.
- Real symptom:
  A follow-up `turn/steer` sent immediately after `turn/start` may fail with
  `no active turn to steer`.
- Why it matters:
  IM UX wants fast steering. Users naturally send a second message while the
  first one is still starting up.
- Current handling:
  We retry steer once after a short delay before falling back.

### 1.2 `final_answer` is not the same thing as canonical turn completion

- Observed behavior:
  Codex can emit a `final_answer`-phase `agentMessage` before the later
  `turn/completed` notification arrives.
- Why it matters:
  The bridge wants to push useful text early, but the real terminal state still
  belongs to `turn/completed`.
- Current handling:
  We may surface final text early as progress, but terminal success/failure is
  still resolved by the later turn status.

### 1.3 Server request identity is different from IM-visible ticket identity

- Observed behavior:
  Approval and user-input requests have server-side request ids, while the IM
  bridge creates its own user-facing ticket ids.
- Why it matters:
  If these two ids are confused, approval works once or twice and then starts
  failing with `no pending request`.
- Current handling:
  The store keeps both ids, and replies are sent back using the original server
  request id.

### 1.4 Some bound threads can become stale or effectively poisoned

- Observed behavior:
  A previously persisted thread may exist in local state but reliably time out
  on `turn/start`.
- Why it matters:
  Users see a healthy selected thread, but new turns never begin.
- Current handling:
  If a bound thread fails to start a turn in the recovery path, we can clear it,
  create a new thread, and retry.

### 1.5 App-server traffic is request-plus-notification, not simple RPC

- Observed behavior:
  Important state comes from both direct request results and later async
  notifications such as `turn/started`, `item/completed`, `turn/completed`, and
  approval requests.
- Why it matters:
  A bridge that only trusts the initial RPC response will look flaky or lose
  state.
- Current handling:
  We maintain a live notification pipeline and project those notifications into
  IM-visible state and messages.

### 1.6 Session portability needs native Codex identity, not bridge-only state

- Observed behavior:
  The bridge can persist its own binding state, but that alone is not enough for
  cross-client continuation.
- Why it matters:
  We want one working-directory session to move across IM bridge, Codex CLI, and
  Codex Desktop.
- Current direction:
  Use native thread/session primitives such as `threadId` and `thread/resume`
  rather than inventing a bridge-only session format.

## 2. Windows problems

These are not Codex protocol problems. They come from running the bridge on
Windows, especially with mixed Python, Node, and desktop-installed Codex
executables.

### 2.1 `codex.exe` versus `codex.cmd` can behave differently

- Observed behavior:
  Launching the wrong Windows-side Codex binary can fail or behave differently
  from the working CLI shim.
- Real symptom:
  Startup failures, permission errors, or surprising behavior when launching
  `app-server`.
- Current handling:
  Prefer the stable installed CLI shim path when spawning Codex on Windows.

### 2.2 Process cleanup matters much more than it first appears

- Observed behavior:
  Old `python -m imcodex` or `codex app-server` processes can remain alive and
  continue listening on old ports.
- Why it matters:
  A user thinks they restarted into new code, but traffic is still handled by an
  older process.
- Current handling:
  We repeatedly verified and cleaned port listeners and child processes before
  restart.

### 2.3 Port ownership is easy to misread during iterative local runs

- Observed behavior:
  `8000` and `8765` may be live even when the expected foreground command is no
  longer visible.
- Why it matters:
  It creates false positives: "the service is up" may really mean "some old
  service is up".
- Current handling:
  We inspect the owning process for the ports, not just whether the port is open.

### 2.4 Path normalization is not optional on Windows

- Observed behavior:
  Different spellings of the same path can appear across app-server responses,
  config, and user input.
- Why it matters:
  Without normalization, the bridge can think one directory is multiple
  projects/cwds.
- Current handling:
  Paths are normalized before project/cwd identity is derived.

### 2.5 Mixed toolchain environment increases ambiguity

- Observed behavior:
  The system involves Python, Node-installed Codex, the desktop Codex app,
  PowerShell, and QQ networking.
- Why it matters:
  Failures can look like a single problem but actually come from the wrong
  executable, wrong environment variables, or a stale background process.
- Practical lesson:
  On Windows, runtime verification has to include executable path, process id,
  open ports, and log files.

## 3. Channel problems

These are issues tied to the IM/QQ channel itself or to the way an asynchronous
coding agent has to be projected into a chat platform.

### 3.1 Production QQ and sandbox QQ are materially different environments

- Observed behavior:
  A bot can be correctly configured in sandbox while the production gateway still
  rejects it, especially around white-listing.
- Real symptom:
  `getAppAccessToken` succeeds, but gateway access fails with IP white-list
  errors on production.
- Current handling:
  For this project we explicitly use the sandbox QQ API base.

### 3.2 Message reply context must survive async completion

- Observed behavior:
  The final Codex answer is often sent well after the inbound QQ message.
- Why it matters:
  If `msg_id` or reply context is lost, the QQ send succeeds less reliably or
  appears detached from the original user message.
- Current handling:
  We persist the latest inbound message id per conversation and reuse it for
  async replies.

### 3.3 `msg_seq` handling is platform-specific and easy to get subtly wrong

- Observed behavior:
  QQ message posting is not just "send text"; reply sequencing matters.
- Why it matters:
  Messages may fail, appear out of place, or behave inconsistently if sequence
  semantics are wrong.
- Current handling:
  The adapter tracks per-conversation message sequence numbers.

### 3.4 IM platforms are bad at raw token streaming

- Observed behavior:
  Dumping every delta into chat creates noise and makes the bot feel broken.
- Why it matters:
  Long model outputs become unreadable and steering becomes harder, not easier.
- Current handling:
  We do not stream every token. Instead, we now prefer chunked progress updates
  when Codex completes an intermediate `agentMessage` item.

### 3.5 Approval UX in IM is naturally awkward

- Observed behavior:
  IM platforms do not give us the same rich approval affordances as a desktop
  Codex UI.
- Why it matters:
  Command approvals quickly become tedious and create too much friction.
- Current handling:
  We added auto-approve support through environment variables so the bridge can
  run in a much more autonomous mode when desired.

### 3.6 Channel identity and Codex identity are different domains

- Observed behavior:
  QQ conversation ids, IM tickets, thread ids, turn ids, and request ids all
  coexist, but they are not interchangeable.
- Why it matters:
  Many subtle bugs come from mixing these identity layers.
- Practical lesson:
  The bridge should keep channel identity, conversation identity, thread
  identity, turn identity, and pending-request identity explicitly separate.

### 3.7 IM semantics should converge on `cwd`, not abstract "project" wording

- Observed behavior:
  In this bridge, "project", "current directory", and "working folder" are
  almost always the same thing from the user's point of view.
- Why it matters:
  Teaching both concepts at once increases friction in chat.
- Current direction:
  Move user-facing terminology toward a single `cwd` concept and keep internal
  project ids as an implementation detail only.

## Summary

The main lesson is that this bridge is not "just a bot adapter". It sits at the
intersection of:

- Codex session and app-server semantics
- Windows process/runtime quirks
- QQ channel behavior and IM UX constraints

If a future issue appears, first classify it into one of these three groups.
That usually narrows the investigation path much faster.
