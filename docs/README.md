# IMCodex Docs

Current documentation is intentionally small.

Use these files as the source of truth for the current implementation:

- [ADR 0001](./adr/0001-native-thin-bridge.md)
  Native-first thin bridge ownership and architecture.
- [Codex Native Capabilities](./codex-native-capabilities.md)
  What native Codex owns versus what `imcodex` still owns.
- [Message Contract](./message-contract.md)
  Current webhook / IM-visible message classes and request identity semantics.
- [Deployment](./deployment.md)
  Current runtime and environment configuration.
- [Dedicated Core Architecture](./dedicated-core-architecture.md)
  Why bridge and native core are being split, and what that buys us.
- [Restart Executor](./restart-executor.md)
  External bridge restart flow for hot reloads without self-stop dead-ends.
- [Command Roadmap](./command-roadmap.md)
  Current `/help` surface, command phases, and native-vs-bridge command boundaries.
- [Desktop Thread Replay Ordering Issue](./desktop-thread-replay-ordering.md)
  Why old IM messages can appear near the bottom of a Desktop transcript after thread replay.
- [Logging And Observability](./logging-observability.md)
  Per-instance runtime archives, structured event logs, and health snapshots.
- [Debug Harness](./debug-harness.md)
  Start isolated instances, send synthetic messages, inspect runtime state, and run built-in repro scenarios.
- [Debug Finding: Restart Gap](./debug-restart-gap.md)
  Evidence that stopping the bridge leaves a restart gap with no automatic recovery.
- [Debug Finding: Approval Stall](./debug-approval-stall.md)
  Evidence that stale approval routing can leave an active turn stuck in progress.
