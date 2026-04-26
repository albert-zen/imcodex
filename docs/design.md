# Design

Status: Draft

## Purpose

`imcodex` is a thin IM bridge over native Codex.

The project exists to let IM conversations drive a real Codex core without
re-implementing a second agent runtime, a second approval engine, or a second
thread model inside the bridge.

## Core Product Shape

The intended runtime has four practical surfaces:

- transport adapters under `imcodex.channels`
- bridge logic under `imcodex.bridge`
- native Codex protocol integration under `imcodex.appserver`
- a thin composition/runtime shell that wires them together

The bridge should feel conversational in IM, but the native Codex core remains
the authority for:

- thread lifecycle
- turn lifecycle
- approval and request identity
- model / permission / reasoning semantics

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
- IM-only visibility preferences
- minimal request routing needed to complete native flows

If a change introduces a new local source of truth for something native Codex
already owns, that is a design smell and should be challenged.

## Dedicated Core Direction

The preferred runtime shape is:

- a long-lived dedicated Codex core
- a separately restartable IM bridge
- native recovery first, local cleanup only as a fallback

This direction is preferred over bridge-managed private cores because it keeps
native thread and approval state alive across bridge restarts and makes
observability clearer.

See also:

- [Product Behavior Spec](product-behavior-spec.md)
- [System Constraints Spec](system-constraints-spec.md)
- [ADR 0001](adr/0001-native-thin-bridge.md)
