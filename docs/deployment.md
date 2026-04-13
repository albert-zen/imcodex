# Deployment

This project is currently distributed as a source deployment, not as a packaged
desktop installer or standalone binary.

## Requirements

- Windows with PowerShell 7 recommended
- Python 3.13 or newer
- A working `codex` installation on the target machine
- `codex app-server` must be available from that `codex`
- If using QQ, valid QQ bot credentials for that machine and network

## Quick Start

```powershell
git clone https://github.com/albert-zen/imcodex.git
cd imcodex
Copy-Item .env.example .env
pip install -e .
python -m imcodex
```

You can also use the helper scripts:

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
IMCODEX_APP_SERVER_HOST=127.0.0.1
IMCODEX_APP_SERVER_PORT=8765
IMCODEX_CODEX_BIN=codex
```

For QQ, also configure:

```env
IMCODEX_QQ_ENABLED=1
IMCODEX_QQ_APP_ID=...
IMCODEX_QQ_CLIENT_SECRET=...
IMCODEX_QQ_API_BASE=https://sandbox.api.sgroup.qq.com
IMCODEX_DEFAULT_PERMISSION_PROFILE=review
```

## What Must Already Work On The Target Machine

- `python --version` returns 3.13 or newer
- `codex --help` works
- `codex app-server --help` works
- If QQ is enabled:
  - the credentials are valid
  - the target machine can reach QQ endpoints
  - any required sandbox or IP whitelist configuration is already set up

## Common Deployment Steps

1. Install Python.
2. Install and authenticate `codex`.
3. Clone this repository.
4. Copy `.env.example` to `.env` and fill it in.
5. Run `pip install -e .`.
6. Run `pwsh -File .\scripts\doctor.ps1`.
7. Start the service with `pwsh -File .\scripts\start.ps1`.

## Notes

- The bridge stores state in `IMCODEX_DATA_DIR`.
- The bridge starts its own local `codex app-server`.
- The HTTP API and the local app-server port must both be free on the target
  machine.
- `doctor.ps1` is intended for preflight checks, not for deep production
  monitoring.
