# ADR 0003: Current Thread subscriptions and SDK-owned fan-out

## Status

Accepted. This decision supersedes the remembered-recipient behavior for
implicit Thread delivery in ADR 0002. Explicit operator delivery to a named
Conversation is unchanged.

## Context

An IM Conversation has one current Agent Thread, while the same Thread may be
open from several IM Conversations and other native clients. Treating a stale
historical recipient as an independent delivery authority lets an IM
Conversation continue receiving a Thread after `/new`, `/pick`, or `/exit`.
It also collapses valid multi-Conversation observation into a single
`Thread -> recipient` value.

The SDK already separates the forward binding from its projection routes,
owns one Application subscription worker per Thread, supports multiple
`ThreadProjectionRoute` destinations, filters active routes under
`foreground_only`, and can proactively deliver through a
`ThreadRouteDeliveryTarget`.

## Decision

- Each IM Conversation has at most one current SDK Conversation binding.
- Binding the Conversation to a different Thread atomically replaces that
  forward selection through the SDK revision CAS.
- IMCodex selects SDK `foreground_only` projection policy. A stored route is
  active only while its Conversation binding selects that same Thread.
- One Thread may have multiple active IM routes. The SDK uses one native
  Application subscription worker and fans output out to every active route.
- `/new`, `/pick`, `/fork`, and `/exit` remain IMCodex product commands, but
  their binding and observation effects use typed SDK Gateway operations.
- Implicit agent delivery by native Thread ID uses SDK
  `ThreadRouteDeliveryTarget` and therefore targets all current active IM
  routes for that Thread. It does not consult a product-owned last-recipient
  map. Explicit operator delivery may still name one Conversation.
- The one-time legacy migration creates one route per current imported
  `(Conversation, Thread)` edge. It never replaces another Conversation's
  route for the same Thread and never lets legacy JSON overwrite an existing
  SDK binding.
- Historical inactive routes may remain as rebuildable checkpoint state, but
  under `foreground_only` they grant no delivery authority. Thread-route
  proactive delivery resolves only SDK active routes.

## Consequences

Switching IM-A from T1 to T2 stops all automatic and implicit Thread-targeted
delivery from T1 to IM-A. Other Conversations still bound to T1 continue to
receive T1 output. IMCodex no longer persists a second `Thread -> recipient`
routing truth, and no extra Application subscription or delivery runtime is
introduced.
