# Workflow

Status: Draft

## Purpose

This document describes the intended human/agent working loop for `imcodex`.

It complements the product and system specs by focusing on how changes should
be investigated, implemented, reviewed, and closed.

## Recommended Runtime Workflow

For day-to-day work, prefer the external native App Server path described in
[startup.md](startup.md):

1. start the native App Server daemon
2. start the IM bridge against its `unix://` control socket
3. debug bridge behavior without replacing native thread/turn truth

This keeps the bridge restartable while leaving the native core alive.

When working on startup, supervision, or reconnect logic, preserve the explicit
`stdio://` compatibility target without turning it into a fallback. Treat Unix
and TCP WebSocket as transports of the same external ownership model.

## Agent-Led Change Loop

Run AgentKit through the repository launcher instead of relying on a global
command or shell `PATH` configuration:

```bash
scripts/agentkit status
```

On native Windows, use `scripts\agentkit.cmd status`. The launcher prefers the
repository virtual environment and invokes `python -P -m agentkit`. This keeps
module dispatch consistent across macOS, Windows, and conda environments without
requiring the `agentkit` console script on the shell `PATH`.
Set `AGENTKIT_PYTHON` only when an explicit interpreter is required. If the
selected interpreter does not contain AgentKit, install the optional dependency
from the repository root. Prefer `uv pip`, which can install into a virtual
environment that does not contain pip itself:

```bash
uv pip install --python .venv/bin/python -e ".[agentkit]"
```

On native Windows, use `.venv\Scripts\python.exe` as the `--python` value. If
`AGENTKIT_PYTHON` selects a different interpreter, pass that path instead.
Without uv, use the selected interpreter's existing pip. If pip is absent and
`ensurepip` is available, bootstrap it with
`<selected-python> -m ensurepip --upgrade`, then run
`<selected-python> -m pip install -e ".[agentkit]"`.

The launcher does not install dependencies or run `uv` automatically, so using
it cannot create or update `uv.lock` as a side effect.

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

## Commit Messages

Commit messages should follow a small Conventional Commits shape:

```text
<type>(<scope>): <short imperative summary>
```

Use `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, or `ci` as the type.
Scopes should usually match repository components such as `appserver`,
`bridge`, `channels`, `runtime`, `docs`, `ci`, or `agentkit`. Keep the subject
under 72 characters. For non-trivial changes, add a short body that explains
why the change exists and which checks were run.

The repository includes `.gitmessage` as an optional template for local use.
Enable it with:

```powershell
git config commit.template .gitmessage
```

## Continuous Integration

Pull requests and pushes to `main` should pass the GitHub Actions CI workflow.
The baseline CI gate installs the package with development dependencies on
Python 3.13 and runs the full `python -m pytest` regression suite.

CI also runs an advisory AgentKit check. Its isolated job installs the
repository's `agentkit` optional dependency and invokes the resulting console
script directly; the repository launcher remains the required entry point for
local and agent-led work, where shell `PATH` cannot be assumed. The advisory
job is non-blocking, so AgentKit availability or maintainability warnings do not
prevent merging a PR.
