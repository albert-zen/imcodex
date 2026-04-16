# Command Roadmap

This document defines the current `/help` surface and the command roadmap for `imcodex`.

## Design Principles

- `imcodex` should align with native Codex semantics first.
- The bridge only owns IM-specific routing and visibility settings.
- Native protocol methods should be accepted into the bridge even before they get dedicated command UX.
- User commands fall into two groups:
  - native Codex capability mappings
  - IM-specific bridge configuration
- Thread workflow commands are phase two, but their native methods should already be reachable through the bridge.

## `/help` Phase One

```text
IMCodex Commands

Thread
/cwd <path>
/status
/new
/stop
/threads [query] [--page N] [--all]
/thread attach <thread-id-or-name>
/thread read

Model & Config
/model <name|default>
/models
/config read [key]
/config write <key> <json-value>
/config batch <json>

Requests
/requests
/approve [request-id-or-prefix]
/deny [request-id-or-prefix]
/cancel [request-id-or-prefix]
/answer [request-id-or-prefix] key=value ...

View
/view minimal|standard|verbose
/show commentary|toolcalls|system
/hide commentary|toolcalls|system

Advanced
/native help
```

## Phase One Scope

### Thread Path

- `/cwd <path>`
- plain text input
- `/status`
- `/new`
- `/stop`

### Thread Query And Binding

- `/threads [query] [--page N] [--all]`
- `/thread attach <thread-id-or-name>`
- `/thread read`

### Model And Native Config

- `/model <name|default>`
- `/models`
- `/config read [key]`
- `/config write <key> <json-value>`
- `/config batch <json>`

### Pending Request Round-Trip

- `/requests`
- `/approve`
- `/deny`
- `/cancel`
- `/answer`

### IM Visibility Controls

- `/view minimal|standard|verbose`
- `/show commentary|toolcalls|system`
- `/hide commentary|toolcalls|system`

### Advanced Escape Hatch

- `/native help`

## Phase Two Scope

All new workflow commands should live under `/thread ...` instead of creating scattered top-level commands.

- `/thread fork <thread-id>`
- `/thread archive [thread-id]`
- `/thread unarchive <thread-id>`
- `/thread rollback <turn-count>`
- `/thread name <text>`
- `/thread compact`
- `/thread shell <command>`

More thread workflow commands can be added later as native Codex support expands, but the namespace rule is fixed now.

## `/model` Semantics

- `/model <name>` sets the native Codex default model.
- `/model default` clears the native default-model override and falls back to native config resolution.
- Phase one does not add a dedicated one-turn model override command.
- If a future native workflow needs one-turn override behavior, it should be exposed through `/native call` first.

## `/view` And `/show|/hide`

- `/view minimal|standard|verbose` remains a bridge-owned IM visibility preset.
- `/show` and `/hide` remain bridge-owned fine-grained overrides.
- The currently supported visibility dimensions are:
  - `commentary`
  - `toolcalls`
  - `system`
- These are IM-specific bridge settings and are not written back into native Codex config.

## `/native` Positioning

`/native` is the advanced escape hatch that keeps the full native protocol surface usable before every method gets dedicated UX.

Phase-one documentation should treat these as advanced commands:

- `/native call <method> <json>`
- `/native respond <request-id-or-prefix> <json>`
- `/native error <request-id-or-prefix> <code> <message> [data-json]`
- `/native requests`
- `/native events [filters...]`

## Delivery Order

1. Expand the protocol map so native notifications, requests, and client calls are accepted without lossy filtering.
2. Land the phase-one `/help` surface and the high-frequency commands above.
3. Promote phase-two thread workflow operations into dedicated `/thread ...` commands once their bridge routing is already in place.
