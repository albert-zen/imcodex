# Workflow

Status: Draft

## Purpose

This document describes the intended human/agent working loop for `imcodex`.

It complements the product and system specs by focusing on how changes should
be investigated, implemented, reviewed, and closed.

## Recommended Runtime Workflow

For day-to-day work, prefer the dedicated core path described in
[startup.md](startup.md):

1. start a long-lived dedicated Codex core
2. start the IM bridge against that core
3. debug bridge behavior without replacing native thread/turn truth

This keeps the bridge restartable while leaving the native core alive.

## Agent-Led Change Loop

For repository-changing work:

1. clarify the task and the layer that should own the change
2. check native Codex behavior before inventing bridge behavior
3. implement the thinnest bridge-side translation that satisfies the need
4. run focused tests, then full regression when the change is cross-cutting
5. update durable docs when the change affects product intent, constraints, or
   operator workflow

When the layer ownership is unclear, that is usually a sign the design needs to
be simplified before more code is added.

## Debugging Guidance

When a runtime failure is hard to explain:

- reproduce it with the debug harness when possible
- add observability before adding more local behavior
- prefer evidence from native protocol behavior, logs, and restart traces over
  speculative bridge state

This is especially important for:

- approval handling
- turn recovery
- restart semantics
- duplicate inbound or outbound delivery

## Review Expectations

Changes that affect public behavior, data flow, runtime architecture, or bridge
state should get a clean-context review loop before human closeout.

When review findings reveal missing durable intent, update the relevant docs
instead of leaving the reasoning trapped in a transient conversation.
