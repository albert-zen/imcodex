# IMCodex Message Contract

This document defines the bridge-visible message contract for `imcodex`.

It focuses on what an IM or webhook integrator can rely on, not on internal
Codex notification details.

## 1. Message Classes

`imcodex` emits five user-visible message classes:

- `accepted`
  Immediate acknowledgement for a normal natural-language request.
- `status`
  Immediate setup or thread-management feedback such as `/new` and
  `/thread attach`.
- `command_result`
  Immediate result for informational commands and approval responses.
- `turn_progress`
  Asynchronous partial progress from a non-final agent message.
- `turn_result`
  Asynchronous terminal content for a completed answer, or terminal status
  output for failed/interrupted turns.

Two more asynchronous request classes are emitted when Codex needs user action:

- `approval_request`
- `question_request`

## 2. Natural-Language Turn Lifecycle

For a normal text request, the logical lifecycle is:

1. Immediate sync ack: `accepted`
2. Optional async progress: `turn_progress`
3. Optional async approval/question: `approval_request` or `question_request`
4. Final async terminal message: `turn_result`

Important notes:

- Successful `final_answer` content is emitted as `turn_result`
- A later `turn/completed(status=completed)` updates local state only and does
  not emit a duplicate terminal message
- Failed or interrupted turns still emit a terminal `turn_result`

## 3. Slash Command Contract

Slash commands do not use `accepted`.

They return one immediate message:

- `status` for thread-management/setup flows such as `/new` and `/thread attach`
- `command_result` for informational commands such as `/status` and `/threads`
- `error` for invalid or blocked commands

Examples:

- `/status` -> `command_result`
- `/threads` -> `command_result`
- `/new` -> `status`
- `/thread attach <thread-id>` -> `status`
- `/approve <ticket>` -> `command_result`

## 4. Transport Expectations

Webhook clients should treat the HTTP response from
`POST /api/channels/webhook/inbound` as the sync channel for immediate messages.

Channel sinks such as QQ should treat outbound push delivery as the channel for
asynchronous progress, approvals, questions, and final results.

The logical lifecycle is stable, but integrators should not assume strict wall-
clock ordering between the immediate ack and later asynchronous pushes when the
transport itself is long-lived and asynchronous.

The safe client rule is:

- render the immediate sync response if present
- merge later async messages by conversation id
- treat `turn_result` as terminal for user-visible content

## 5. Tool Activity

In this phase, tool activity is only part of the contract at a high level:

- command approvals -> `approval_request`
- tool/user input prompts -> `question_request`
- intermediate prose from Codex -> `turn_progress`
- final prose from Codex -> `turn_result`

Rich UI treatment of command execution and file changes is intentionally left
for a later iteration.
