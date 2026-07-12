#!/usr/bin/env bash
set -euo pipefail

repo_root="${PWD}"
while [[ ! -x "${repo_root}/scripts/agentkit" ]]; do
    parent="$(cd -- "${repo_root}/.." && pwd)"
    if [[ "${parent}" == "${repo_root}" ]]; then
        echo "AgentKit Stop hook could not find scripts/agentkit above ${PWD}." >&2
        exit 127
    fi
    repo_root="${parent}"
done

exec "${repo_root}/scripts/agentkit" codex-stop-hook \
    --log "${repo_root}/.agentkit/codex-stop-hook.log"
