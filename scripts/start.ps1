Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path ".env")) {
    Write-Warning "No .env file found. Copy .env.example to .env and fill it in before production use."
}

$python = if ($env:IMCODEX_PYTHON) { $env:IMCODEX_PYTHON } else { "python" }

Write-Host "Starting imcodex from $repoRoot"
Write-Host "Using Python: $python"

& $python -m imcodex
