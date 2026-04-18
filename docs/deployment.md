# Deployment

This project is currently deployed from source and integrates with native
Codex `app-server` in multiple modes:

- preferred architecture: connect to a dedicated long-lived websocket core
- compatible/shared mode: connect to an already-running websocket app-server
- fallback/debug mode: spawn a local `codex app-server --listen stdio://`

## Requirements

- Windows with PowerShell 7 recommended
- Python 3.13 or newer
- `codex-cli 0.120.0` or newer
- `codex app-server --help` works on the target machine
- If using QQ, valid QQ bot credentials and network access

## Quick Start

```powershell
git clone https://github.com/albert-zen/imcodex.git
cd imcodex
Copy-Item .env.example .env
pip install -e .
python -m imcodex core start --port 8765
$env:IMCODEX_CORE_MODE = "dedicated-ws"
$env:IMCODEX_CORE_URL = "ws://127.0.0.1:8765"
python -m imcodex
```

Optional helper scripts:

```powershell
pwsh -File .\scripts\doctor.ps1
pwsh -File .\scripts\start.ps1
```

## Required Configuration

At minimum, configure these in `.env`:

```env
IMCODEX_DATA_DIR=D:\services\imcodex\.imcodex-data
IMCODEX_HTTP_HOST=0.0.0.0
IMCODEX_HTTP_PORT=8000
IMCODEX_CODEX_BIN=codex
IMCODEX_CORE_MODE=dedicated-ws
IMCODEX_CORE_URL=ws://127.0.0.1:8765
IMCODEX_SERVICE_NAME=imcodex
```

For QQ, also configure:

```env
IMCODEX_QQ_ENABLED=1
IMCODEX_QQ_APP_ID=...
IMCODEX_QQ_CLIENT_SECRET=...
IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com
```

## What Must Already Work

- `python --version` returns 3.13 or newer
- `codex --version` returns 0.120.0 or newer
- `codex app-server --help` works
- If QQ is enabled:
  - credentials are valid
  - the target machine can reach QQ endpoints
  - any required QQ whitelist or sandbox setup is already in place

## Operational Notes

- The bridge stores only minimal IM-specific state in `IMCODEX_DATA_DIR`.
- Native Codex remains the source of truth for thread, turn, request, model,
  and permission state.
- `dedicated-ws` is now the recommended topology for serious local use.
- `spawned-stdio` still exists, but should be treated as fallback/debug-only.
- The bridge can also be restarted externally with:

```powershell
python -m imcodex ops restart --launch-snapshot <path>
```

- `doctor.ps1` is intended for preflight checks, not deep monitoring.
