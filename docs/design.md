# Design

Status: Draft

## Purpose

`imcodex` is a thin IM bridge over native Codex.

The project exists to let IM conversations drive a real Codex core without
re-implementing a second agent runtime, a second approval engine, or a second
thread model inside the bridge.

## Core Product Shape

The intended runtime has five practical surfaces:

- transport adapters under `imcodex.channels`
- bridge logic under `imcodex.bridge`
- native Codex protocol integration under `imcodex.appserver`
- a loopback-only configuration presentation under `imcodex.admin`
- a thin composition/runtime shell that wires them together

`imcodex.admin` belongs to the runtime/composition side of the architecture. It
projects native settings without owning them and manages only the explicit
bridge/channel configuration schema; lower layers do not depend on it.

The bridge should feel conversational in IM, but the native Codex core remains
the authority for:

- thread lifecycle
- turn lifecycle
- approval and request identity
- model / permission / reasoning semantics

The built-in transport set currently includes QQ, Telegram, Feishu/Lark, and
experimental Tencent iLink Weixin. These are peer adapters over the same
bridge contract; no channel owns a separate Codex agent or thread runtime.

Remote adapters share one optional access-restriction model. Platform delivery
is the default scope; stable user and conversation IDs can narrow that scope,
and `any`/`all` selects how multiple active dimensions combine. This is an IM
transport gate, not a second permission engine and not a prerequisite in the
first connection flow.

## Visual Identity

The product mark is a geometric `IM` monogram: a teal `I`, an ink-coloured
folded `M`, and a teal lower-left cutout. It represents the IM side joining a
native Codex path without turning the bridge into a separate agent product.

Use the mark as a responsive identity system: the vertical primary logo for
large brand surfaces, the horizontal lockup for product chrome, and the mark
alone for favicons or compact controls. Keep it flat, vector, and
monochrome-compatible. Do not replace it with generic chat bubbles, bot faces,
rounded app-icon containers, network-node clip art, gradients, or shadows.

## Native-First Rule

Native Codex source code and protocol behavior are the first source to inspect
before adding bridge behavior.

When implementing a new capability:

1. check whether native Codex already provides it
2. integrate with that native capability directly when it exists
3. only add bridge-owned state or workflow when native Codex does not expose
   the needed behavior and IM still requires it

This keeps `imcodex` thin, inspectable, and easier to recover.

## Bridge-Owned Concerns

The bridge may keep only IM-specific state that native Codex does not own, such
as:

- channel and conversation bindings
- bootstrap context before a native thread exists
- reply context needed by a transport adapter
- the last admitted stable sender ID needed to recheck current channel policy
  before projecting later native output
- IM-only visibility preferences
- minimal request routing needed to complete native flows
- platform transport cursors and reply tokens required to resume an IM
  protocol, such as Telegram update offsets and Weixin context tokens

If a change introduces a new local source of truth for something native Codex
already owns, that is a design smell and should be challenged.

Transport credentials and cursors are not native Codex state. A channel may
persist them only when its platform protocol requires them, using private files
that never enter launch snapshots or normal user-visible diagnostics.

## App Server Runtime Direction

The preferred runtime shape for day-to-day IM use is:

- a long-lived native Codex App Server
- a separately restartable IM bridge
- native recovery first, local cleanup only as a fallback

This direction is preferred over bridge-managed private cores because it keeps
native thread and approval state alive across bridge restarts and makes
observability clearer.

The normal product shape has one external App Server target. Unix and TCP are
transport facts, not different ownership modes, and the bridge cannot observe a
meaningful difference between the old `dedicated-ws` and `shared-ws` labels.
The target URL is therefore the canonical configuration.

`stdio://` remains an explicit bridge-child compatibility target for tests and
older installations. It MUST NOT be selected as a fallback after an external
target fails. Native Windows keeps an external two-process shape through the
project-managed detached TCP App Server until native daemon lifecycle is
available there. Legacy mode names are accepted only at the configuration
boundary and are normalized before runtime behavior begins.

See also:

- [Product Behavior Spec](product-behavior-spec.md)
- [System Constraints Spec](system-constraints-spec.md)
- [ADR 0001](adr/0001-native-thin-bridge.md)
