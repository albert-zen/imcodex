# Desktop Thread Replay Ordering Issue

This document records a native Codex Desktop / TUI issue observed while validating cross-entry thread continuity between `imcodex` and Codex Desktop.

## Summary

When QQ / IM messages are written into a native Codex thread that is already loaded in Codex Desktop, those messages can later appear at the bottom of the Desktop transcript as if they were newly appended, even though their original timestamps are much earlier.

This is not a bridge persistence failure.

- `imcodex` writes the messages into the correct native thread.
- native rollout persistence keeps the correct original timestamps.
- native `thread/resume` returns the QQ turns in the correct historical order.
- the surprising ordering appears during Desktop/TUI local replay of an already-loaded thread.

## User-Visible Symptom

Observed behavior:

- the user sent two QQ messages around `2026-04-17 01:56` and `01:58` Asia/Shanghai
- later, after switching back to the same thread in Codex Desktop, those old QQ messages appeared near the bottom of the transcript
- the Desktop view did not present them as clearly old messages with a separate date break
- visually, they looked like recently appended messages

This creates a misleading cross-surface experience:

- rollout history says the messages are old
- Desktop transcript makes them look new

## Evidence

### 1. Rollout persistence is correct

The two QQ messages are stored in the same native rollout file with the expected timestamps:

- [rollout-2026-04-17T00-07-44-019d970c-6253-78d0-899d-243239ffeb76.jsonl](</C:/Users/xmly/.codex/sessions/2026/04/17/rollout-2026-04-17T00-07-44-019d970c-6253-78d0-899d-243239ffeb76.jsonl:942>)

Relevant entries:

- line `942`: user message `漂亮啊，有点感动。终于算比较好的打通codex生态体验了。`
- line `951`: user message `话说我这里显示很多thread的state是notloaded是啥意思？`

Recorded timestamps:

- `2026-04-16T17:56:59.642Z`
- `2026-04-16T17:58:19.746Z`

These correspond to `2026-04-17 01:56:59` and `01:58:19` in Asia/Shanghai.

### 2. Native `thread/resume` order is also correct

Probing the full native `thread/resume` response for thread `019d970c-6253-78d0-899d-243239ffeb76` showed:

- total turns returned: `33`
- first QQ message appears at turn index `14`
- second QQ message appears at turn index `15`

So native app-server does not move these QQ messages to the tail. They remain in historical position inside the canonical resumed turns list.

### 3. Desktop/TUI replays turns first, then buffered events

Relevant upstream files in the vendored source tree:

- [codex-upstream/codex-rs/tui/src/app.rs](D:/desktop/imcodex/codex-upstream/codex-rs/tui/src/app.rs)
- [codex-upstream/codex-rs/tui/src/chatwidget.rs](D:/desktop/imcodex/codex-upstream/codex-rs/tui/src/chatwidget.rs)

Important behavior:

- `ThreadEventStore::set_session()` stores `turns` as the thread snapshot
- later notifications are pushed into `buffer` via `push_notification()`
- `replay_thread_snapshot()` rebuilds a thread by:
  - replaying `snapshot.turns`
  - then replaying `snapshot.events`
- `ChatWidget::replay_thread_turns()` renders turns in the order received, without further timestamp re-sorting

That means the odd Desktop ordering is not caused by `thread/resume` itself. It comes from how an already-loaded thread is reconstructed from:

- older snapshot turns
- newer buffered events

## Root Cause Hypothesis

The most likely root cause is:

1. Desktop loaded the thread earlier and captured a local `turns` snapshot.
2. Later, QQ / IM wrote new completed user turns into the same native thread.
3. Desktop tracked those cross-entry updates as buffered notifications / replay events instead of materializing them back into the canonical local `turns` snapshot.
4. When the user later switched back to the thread, Desktop replayed:
   - old `turns`
   - then buffered events
5. Those QQ messages therefore appeared visually at the bottom, even though the canonical thread history places them earlier.

In short:

- canonical native history is correct
- Desktop local replay state is stale
- buffered event replay makes old cross-entry messages look new

## Why This Matters For `imcodex`

`imcodex` relies on native Codex thread continuity.

If Desktop presents cross-entry messages in a misleading order:

- users lose trust in cross-surface continuity
- old IM messages can look like recent messages
- debugging context and memory behavior become confusing

This is especially noticeable when:

- the same thread is used from both Desktop and IM
- the Desktop thread remains loaded for a long time
- the user later switches back and expects stable chronological rendering

## Current Status

No bridge code change is proposed here.

Current conclusion:

- this is not an `imcodex` persistence bug
- this is not a native `thread/resume` ordering bug
- this is likely a native Codex Desktop / TUI snapshot-vs-buffer replay bug

## Likely Upstream Fix Directions

Any real fix likely belongs in native Desktop / TUI. Plausible approaches:

1. Materialize completed cross-entry thread updates into `ThreadEventStore.turns`

- when a completed foreign turn arrives, merge it into the local canonical turns snapshot
- avoid leaving it only in buffered replay state

2. Refresh canonical turns before replaying a switched-back loaded thread

- before reconstructing the transcript, fetch a fresh canonical view from app-server
- then use that refreshed turns list as the snapshot base

3. Improve transcript rendering cues

- even if replay ordering remains unchanged, clearer date / source separators would reduce confusion
- this would be a UX mitigation, not a full correctness fix

## Recommendation

Treat this as an upstream Codex Desktop / TUI issue.

`imcodex` should continue to:

- write to the correct native thread
- preserve native-first ownership of history
- avoid inventing a competing thread history model just to patch over Desktop replay behavior

If needed, file an upstream issue with:

- rollout evidence
- native `thread/resume` ordering evidence
- Desktop replay code references
- the reproduction path involving cross-entry QQ / IM messages on an already-loaded Desktop thread
