# Dependency Rules

## Purpose

`imcodex` keeps dependency direction intentionally narrow so the bridge does not
grow into a second local agent framework.

These rules are both architectural intent and a practical review aid.

## Practical Layers

For AgentKit and review purposes, the codebase can be read as six layers:

1. `support`
2. `state`
3. `appserver`
4. `bridge`
5. `channels`
6. `runtime`

## Allowed Direction

### Support

Shared foundations such as config, models, and observability.

- imports nothing from higher layers

### State

Small persisted bridge state and active observability runtime state that other
layers may consult.

- may import `support`

### Appserver

Native Codex protocol integration and dedicated-core transport handling.

- may import `support`
- may import `state`
- must not depend on `bridge` or `channels`

### Bridge

Conversation routing, command handling, projection, and minimal bridge state.

- may import `support`
- may import `state`
- may import `appserver`
- must not depend on `channels`

### Channels

Transport adapters and outbound sinks.

- may import `support`
- may import `state` when channel-safe tracing or message metadata requires it
- must not depend on `bridge` or `appserver`

### Runtime

Composition, startup, ops, and debug harness entry surfaces.

- may import all lower layers in order to wire them together
- should not become a new business layer

## Existing Test Enforcement

Repository tests already enforce the core one-way rules in
`tests/test_architecture.py`.

AgentKit configuration should stay aligned with those same boundaries instead
of inventing a parallel architecture story.
