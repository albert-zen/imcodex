#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "${repo_root}"

load_dotenv() {
    local dotenv_path="${repo_root}/.env"
    [[ -f "${dotenv_path}" ]] || return 0

    while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
        local line key value
        line="${raw_line#"${raw_line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n "${line}" && "${line}" != \#* && "${line}" == *=* ]] || continue

        key="${line%%=*}"
        value="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"

        [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [[ -z "${!key+x}" ]]; then
            export "${key}=${value}"
        fi
    done < "${dotenv_path}"
}

activate_conda_env() {
    [[ -n "${IMCODEX_CONDA_ENV:-}" ]] || return 0

    local conda_sh=""
    if [[ -n "${CONDA_EXE:-}" ]]; then
        conda_sh="$(cd -- "$(dirname -- "${CONDA_EXE}")/.." && pwd)/etc/profile.d/conda.sh"
    fi

    for candidate in \
        "${conda_sh}" \
        "${HOME}/miniconda3/etc/profile.d/conda.sh" \
        "${HOME}/anaconda3/etc/profile.d/conda.sh" \
        "/opt/homebrew/Caskroom/miniconda/base/etc/profile.d/conda.sh" \
        "/opt/anaconda3/etc/profile.d/conda.sh"; do
        if [[ -f "${candidate}" ]]; then
            # shellcheck source=/dev/null
            source "${candidate}"
            conda activate "${IMCODEX_CONDA_ENV}"
            return 0
        fi
    done

    echo "IMCODEX_CONDA_ENV is set to '${IMCODEX_CONDA_ENV}', but conda.sh was not found." >&2
    echo "Set IMCODEX_PYTHON to an explicit Python path, or initialize conda for this shell." >&2
    return 1
}

port_is_listening() {
    local port="$1"
    "${python}" - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.5)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", port)) == 0 else 1)
PY
}

wait_for_core() {
    local port="$1"
    local deadline="${IMCODEX_CORE_START_TIMEOUT:-30}"
    local waited=0

    while (( waited < deadline )); do
        if port_is_listening "${port}"; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo "Dedicated core on ${core_url} did not become ready within ${deadline}s." >&2
    return 1
}

load_dotenv
activate_conda_env

if [[ -n "${IMCODEX_PYTHON:-}" ]]; then
    python="${IMCODEX_PYTHON}"
elif [[ -x "${repo_root}/.venv/bin/python" ]]; then
    python="${repo_root}/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
    python="python"
elif command -v python3 >/dev/null 2>&1; then
    python="python3"
else
    python="python"
fi

core_mode="${IMCODEX_CORE_MODE:-dedicated-ws}"
core_url="${IMCODEX_CORE_URL:-}"
core_port="${IMCODEX_CORE_PORT:-}"
app_server_url="${IMCODEX_APP_SERVER_URL:-}"

if [[ -z "${core_port}" && "${core_url}" =~ ^ws://(127\.0\.0\.1|localhost):([0-9]+)$ ]]; then
    core_port="${BASH_REMATCH[2]}"
fi

core_port="${core_port:-8765}"
core_url="${core_url:-ws://127.0.0.1:${core_port}}"

echo "Starting imcodex from ${repo_root}"
echo "Using Python: ${python}"
if [[ -n "${app_server_url}" ]]; then
    echo "App Server target: ${app_server_url}"
else
    echo "Legacy core mode: ${core_mode}"
fi

if [[ -z "${app_server_url}" && "${core_mode}" == "dedicated-ws" ]]; then
    export IMCODEX_CORE_MODE="${core_mode}"
    export IMCODEX_CORE_URL="${core_url}"

    if port_is_listening "${core_port}"; then
        echo "Dedicated core already appears to be listening on ${IMCODEX_CORE_URL}"
    else
        echo "Starting dedicated Codex core on ${IMCODEX_CORE_URL}"
        "${python}" -m imcodex core start --port "${core_port}"
        wait_for_core "${core_port}"
    fi
fi

exec "${python}" -m imcodex
