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
- [Command Roadmap](./command-roadmap.md)
  Current `/help` surface, command phases, and native-vs-bridge command boundaries.
- [Desktop Thread Replay Ordering Issue](./desktop-thread-replay-ordering.md)
  Why old IM messages can appear near the bottom of a Desktop transcript after thread replay.
