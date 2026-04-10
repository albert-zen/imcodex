# IMCodex Native-First Redesign Plan

This document describes the redesign target for the next-generation `imcodex`
core.

It is intentionally architecture-first. The goal is not to preserve the current
bridge at all costs. The goal is to end up with a simpler bridge that behaves
more like a native Codex surface.

## 1. Redesign Goal

The redesign should make `imcodex` feel like:

- a channel adapter for Codex native behavior

and less like:

- a custom agent framework that happens to talk to Codex

That means the bridge should own less policy, less identity, and less workflow
logic than it does today.

It also means we should explicitly prefer native Codex capabilities whenever
they already provide a solid answer, instead of rebuilding the same concepts in
bridge-owned state.

## 2. Non-Goals

The redesign should not optimize for:

- preserving every detail of legacy bridge state
- keeping bridge-owned auto-approve logic as a first-class feature
- maintaining compatibility with every historical wording choice
- preserving legacy message chatter if it exists only to explain internal routing

## 3. The Three Native Questions

Before implementation, we should answer these three questions clearly.

### 3.1 What Is The Native Session Identity?

We need to define the minimum set of data required to continue a session across:

- one IM conversation after restart
- IM bridge to Codex CLI or Desktop
- CLI or Desktop back into the IM bridge

Candidate pieces:

- `threadId`
- `cwd`
- native session path
- persisted-history configuration

Native capabilities we should explicitly evaluate as primary building blocks:

- `thread/list`
- `thread/read`
- `thread/resume`
- `thread.path`
- `persistExtendedHistory`

Design target:

- the bridge persists only what is needed to recover or reattach a native Codex thread

### 3.2 What Is The Native Permission Model?

We should stop centering the design on bridge-owned auto-approve.

Instead, the bridge should expose or select native Codex permission modes built
from:

- `approval_policy`
- `sandbox_policy`
- native approvals reviewer settings when relevant

Design target:

- the IM surface chooses native permission profiles
- the bridge does not invent a second approval architecture unless strictly necessary

### 3.3 What Is The Native Async Message Model?

The current system projects async events into chat, but the long-term model
should be a real message pump with explicit lifecycle rules.

Design target:

- outbound delivery is organized by conversation and turn
- partial output can be pushed safely
- stale or superseded output is suppressed
- final results have clear precedence over intermediate output

## 4. Proposed Target Architecture

The next core should still keep the three-layer shape:

1. `channels`
2. `bridge`
3. `appserver`

But the responsibilities should tighten.

### 4.1 `channels`

Responsibilities:

- translate platform input into unified inbound events
- send unified outbound messages to the platform
- manage platform-specific identity such as reply ids and sequencing

Should not own:

- Codex thread logic
- approval policy logic
- message-state decisions beyond platform formatting

### 4.2 `bridge`

Responsibilities:

- bind channel conversations to native Codex sessions
- interpret user commands
- manage the message pump
- apply user-facing visibility preferences

Should not own:

- a second session model richer than native Codex needs
- a second permission policy model if native Codex already has one
- a registry of thread-to-cwd relationships if native thread metadata already answers it

### 4.3 `appserver`

Responsibilities:

- native JSON-RPC transport
- `thread/start`
- `thread/resume`
- `turn/start`
- `turn/steer`
- `turn/interrupt`
- approval and question replies

Should not own:

- channel semantics
- chat wording
- visibility preferences

## 5. Proposed New Persistent State

The redesigned bridge should store as little as possible.

Minimum candidate state:

- channel id
- conversation id
- selected `cwd`
- selected native `threadId`
- optional user display and visibility preferences
- current outbound sequencing metadata needed by the channel

Anything else should be justified against the question:

- "Why can this not be derived from native Codex state?"

Likely removable legacy ideas:

- bridge-heavy project abstractions beyond normalized `cwd`
- approval state that duplicates native Codex state unnecessarily
- legacy progress flags that belong in the message pump instead

## 6. Tool Visibility Model

Tool visibility should be user-configurable, but not event-shape configurable.

Instead, define a small set of stable categories:

- `progress`
- `plan`
- `search`
- `files`
- `commands`
- `approvals`
- `questions`

Possible user-facing policy:

- `minimal`
- `standard`
- `verbose`
- custom category toggles later

Design target:

- the bridge decides category from native events
- the user chooses which categories appear in chat

## 7. Message Pump Requirements

The redesigned message pump should support:

- per-conversation outbound flow
- per-turn grouping
- throttling
- coalescing of repeated progress
- de-duplication
- stale-turn suppression after steer or interrupt
- final-result precedence
- channel-specific send context attachment

This should be a first-class component, not incidental logic spread across
projector and service code.

## 8. Migration Philosophy

We should not let legacy compatibility dominate the redesign.

Preferred migration rule:

- migrate only what is easy and clean
- drop what preserves the wrong model
- document the break explicitly

This is acceptable because the project is still early and the architectural
gain is worth more than preserving every historical state shape.

## 9. Recommended First Implementation Slice

After design approval, the first build slice should be:

1. implement the new state model
2. implement the message pump
3. implement native permission-profile selection
4. reconnect one channel flow end to end
5. then layer back richer UX

This order prevents us from rebuilding the old behavior under a new directory layout.

## 10. Success Criteria

The redesign is successful if:

- the bridge owns less state than before
- native Codex identity is clearer than before
- permission behavior is more obviously derived from native Codex modes
- long-running turns are easier to surface cleanly in IM
- cross-surface continuation becomes easier to reason about, not harder
- native APIs such as `thread/list`, `thread/read`, and `thread/resume` are central to the design rather than optional support
