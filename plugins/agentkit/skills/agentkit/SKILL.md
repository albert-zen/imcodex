---
name: agentkit
description: Preserve human intent and project maintainability by guiding agents to read durable intent files and persist meaningful design, docs, and test changes.
---

# AgentKit Skill

This repository uses AgentKit.

Invoke it through `scripts/agentkit` on macOS/Linux or
`scripts\agentkit.cmd` on Windows. The repository launcher selects the project
Python and runs `python -P -m agentkit`; do not rely on a global `agentkit`
command or shell `PATH` configuration. The examples below use the POSIX launcher
name.

## What AgentKit Gives You

AgentKit keeps your work tied to durable repo intent. Use it to:

- find the docs and components relevant to a task
- remember the task's closeout gates
- check docs impact and architecture rules
- get lifecycle reminders while you work
- ask for clean-context review before human attention
- close the task as completed or blocked

The skill is an operating guide. For deeper product or architecture intent, read the durable docs that AgentKit returns.

## When To Start A Task

Use the AgentKit task lifecycle for implementation work, documentation edits, hook/plugin changes, generated files, commits, or any task that changes repository state.

Do not start a task for read-only exploration, codebase orientation, answering architecture questions, or lightweight audits with no edits. In those cases, read the relevant docs directly and use `scripts/agentkit status` or `scripts/agentkit remind` only if you need to inspect an already-open task.

If read-only exploration turns into repository-changing work, start or resume the task before making edits so closeout gates apply to the change.

## Repository-Changing Operating Loop

1. Start or resume the task with `scripts/agentkit start`.
2. Read the durable intent sources in the output.
3. If design is missing or ambiguous for product behavior, API, data model, workflow, architecture, or state transitions, ask the human before implementing that part.
4. Implement against tests and the repo's architecture rules.
5. Run `scripts/agentkit check` and read any lifecycle reminder it prints.
6. Run `scripts/agentkit review-guidance` and request clean-context review when expected.
7. Fix meaningful reviewer findings.
8. Run `scripts/agentkit close --review-complete`, or close as blocked with a recorded human question.

## Start Of Task

For repository-changing work, run:

```text
scripts/agentkit start
```

`start` writes repository-local task state under `.agentkit/`. In a read-only audit, orientation pass, or question-answering task, do not run `start`; read this skill and use read-only commands such as `scripts/agentkit status` or `scripts/agentkit remind` only when they help inspect existing state.

If you know the component, run:

```text
scripts/agentkit start --component <name>
```

After discussion clarifies the task, preserve the focus:

```text
scripts/agentkit start --task "<refined task>" --focus-note "<human-approved focus>" --focus-doc <path>
```

Use `scripts/agentkit start --component <name>` when you already know the component. Otherwise, include the task text and let AgentKit infer affected components.

## During Design

Use:

```text
scripts/agentkit intent-guidance --component <name> --change-type <type>
```

Write the actual design content yourself. AgentKit tells you where it belongs.

Useful change-type values include `architecture`, `data_model`, `public_api`, `orchestration`, `workflow`, `tests`, and `docs`.

For docs-only wording tasks, ask the human for design only when the wording changes product meaning, command semantics, public behavior, workflow expectations, or accepted terminology. For local copyedits that preserve meaning, proceed with focused docs checks and review expectations from AgentKit.

## Before Review

Run:

```text
scripts/agentkit check
scripts/agentkit review-guidance
```

If review is expected, spawn or request a clean-context reviewer with the guidance AgentKit returns.

Do not treat review as a transcript storage task. AgentKit only needs the main agent to acknowledge that the review loop was completed for the current diff. If review reveals durable design, risk, or testing knowledge, record that in the repository docs.

For low-risk docs-only wording changes, review may still be expected by local policy. Use `scripts/agentkit review-guidance` to decide. If the change is truly low risk, close with `scripts/agentkit close --skip-review-reason "..."` only when AgentKit allows it.

## Lifecycle Reminders

Use:

```text
scripts/agentkit status
scripts/agentkit remind
```

`status` shows task facts and missing gates. `remind` shows the next action. `scripts/agentkit check` may also include lifecycle reminders.

For a local reminder loop, use:

```text
scripts/agentkit watch
```

The bundled AgentKit plugin already wires Codex Stop-hook reminders through
plugin-owned wrappers that locate the repository launcher from nested working
directories. Do not run `install-codex-watchdog --repo-local` in this
repository: the current upstream installer generates a hook that calls a global
`agentkit` executable and would reintroduce the shell `PATH` dependency that the
launcher removes. If hook wiring must be restored manually, copy the Stop hook
from `plugins/agentkit/hooks.json`; on native Windows, use the equivalent
`scripts\agentkit.cmd codex-stop-hook` command.

If a Stop hook does not appear to run, check `.agentkit/codex-stop-hook.log`. No log usually means Codex did not invoke the hook.

## Close Task

Before ending the task, run:

```text
scripts/agentkit close --review-complete
```

If blocked on human input, run:

```text
scripts/agentkit close --blocked-question "..."
```

Use blocked close when continuing would require an unsupported assumption. Include the human question clearly.
