# IMCodex Message Contract

This document defines the current bridge-visible message contract for
`imcodex`.

It focuses on what an IM or webhook integrator can rely on, not on internal
Codex notification details.

## 1. Message Classes

`imcodex` emits these user-visible message classes:

- `accepted`
  Immediate acknowledgement for a natural-language request that started or
  steered a native turn.
- `status`
  Immediate setup or thread-management feedback such as `/new` and
  `/thread attach`.
- `command_result`
  Immediate result for informational commands such as `/status`, `/threads`,
  and `/thread read`.
- `error`
  Immediate invalid or blocked-command feedback.
- `approval_request`
  Asynchronous request for native approval.
- `question_request`
  Asynchronous request for native user input.
- `turn_progress`
  Asynchronous projected progress from non-final agent output or allowed
  commentary/tool activity.
- `turn_result`
  Asynchronous final visible output for a turn.

## 2. Natural-Language Turn Lifecycle

For a normal text request, the logical lifecycle is:

1. Immediate sync ack: `accepted`
2. Optional async progress: `turn_progress`
3. Optional async approval/question: `approval_request` or `question_request`
4. Final async terminal message: `turn_result`

Important notes:

- Successful `final_answer` content is emitted as `turn_result`
- A later `turn/completed(status=completed)` updates internal runtime state but
  does not emit a duplicate terminal message
- Failed or interrupted turns still emit a terminal `turn_result`
- If a late tool event arrives after a visible final answer, it is suppressed

## 3. Request Identity

Native Codex `requestId` is authoritative.

- Outbound approval/question messages use `request_id`, not `ticket_id`
- The bridge may show a short request handle for readability, but that handle
  is not the source of truth
- `/approve`, `/deny`, `/cancel`, and `/answer` target the native request id
  or a unique prefix
- If exactly one matching pending request exists, `/approve`, `/deny`, or
  `/cancel` may omit the id

## 4. Slash Command Contract

Slash commands do not use `accepted`.

They return one immediate message:

- `status` for setup and thread-management flows such as `/cwd`, `/new`,
  `/thread attach`, `/model`, `/show`, and `/hide`
- `command_result` for informational commands such as `/status`, `/threads`,
  and `/thread read`
- `error` for invalid, missing, ambiguous, or blocked commands

Examples:

- `/status` -> `command_result`
- `/threads` -> `command_result`
- `/new` -> `status`
- `/thread attach <thread-id>` -> `status`
- `/approve <request-id-or-prefix>` -> `command_result`

## 5. Transport Expectations

Webhook clients should treat the HTTP response from
`POST /api/channels/webhook/inbound` as the sync channel for immediate
messages.

Channel sinks such as QQ should treat outbound push delivery as the channel for
asynchronous progress, approvals, questions, and final results.

The safe client rule is:

- render the immediate sync response if present
- merge later async messages by conversation id
- treat `turn_result` as terminal for user-visible content

## 6. Webhook Outbound Shape

Webhook outbound messages keep this envelope:

- `channel_id`
- `conversation_id`
- `message_type`
- `text`
- `request_id`
- `metadata`

`ticket_id` is obsolete in the current implementation.
