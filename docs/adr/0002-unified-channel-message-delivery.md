# ADR 0002: Unified Channel Attachments and Explicit Delivery

## Status

Accepted.

## Context

Images, generic inbound files, automatic native artifacts, and agent-invoked
delivery cross the same transport boundary. Implementing each as a channel
special case would duplicate staging, safety, retry, and result semantics.

QwenPaw demonstrates the value of a small normalized set of outgoing content
parts and a thin `channels send` command. OpenClaw's message-lifecycle work
shows why concrete channel payloads, durable send identity, per-part outcomes,
and explicit ambiguous results should remain visible instead of being hidden
behind an over-general message class.

## Decision

- `InboundMessage` and `OutboundMessage` remain the two channel-neutral
  envelopes. Attachments are concrete `image` or `file` records, not a new
  agent or document-processing framework.
- Channel adapters own platform parsing, authenticated download/upload, reply
  identity, and conversion to/from the neutral envelopes.
- The shared media boundary owns filename normalization, actual-content
  validation, count/size/quota limits, private staging, and expiry.
- Images project to native Codex `localImage`; supported generic files project
  to native `mention(name, path)`. The same sanitized filename and staged local
  path are also included in the native text input so rollout history,
  `thread/read`, recovery, and other Codex clients retain enough context to
  locate the file even when they omit structured mention items. This requires
  the same verified shared filesystem capability and keeps document
  interpretation inside native Codex.
- Explicit delivery uses `python -m imcodex channels send`, which uploads bytes
  to a loopback-only endpoint authenticated with the current bridge instance
  and a private per-process credential. The running bridge uses its existing
  channel adapters and multiplex sink; the command never starts a second
  polling runtime.
- Callers must name a channel and conversation. The bridge applies the current
  access policy and returns a JSON receipt with overall and per-artifact state,
  platform identity when available, and stable delivery identity.
- A caller-generated delivery ID is propagated to adapters. Platforms without
  an idempotency primitive can still duplicate after an ambiguous acceptance;
  receipts report such failures as unconfirmed rather than claiming success.

## Consequences

- Supported generic inbound files are PDF, UTF-8 text, Markdown, and common
  source/config formats. Archives, Office documents, binary text, and unknown
  formats fail visibly until explicitly added to the safe set.
- Managed local files remain bridge transport state with bounded retention;
  their filename and staged-path descriptor persist in native thread input.
- The explicit tool is a delivery interface, not another thread runtime,
  scheduler, policy engine, or durable source of truth.
