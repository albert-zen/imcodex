Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Import-DotEnv {
    $dotenvPath = Join-Path $repoRoot ".env"
    if (-not (Test-Path $dotenvPath)) {
        return
    }

    foreach ($rawLine in Get-Content $dotenvPath) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }

        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")

        if ($key -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            continue
        }
        if ($null -eq [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

function Enable-CondaEnv {
    if (-not $env:IMCODEX_CONDA_ENV) {
        return
    }

    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
    if ($condaCommand -and $condaCommand.CommandType -eq "Function") {
        conda activate $env:IMCODEX_CONDA_ENV
        return
    }

    $candidates = @()
    if ($env:CONDA_EXE) {
        $condaBin = Split-Path -Parent $env:CONDA_EXE
        $condaRoot = Split-Path -Parent $condaBin
        $candidates += Join-Path $condaRoot "shell\condabin\conda-hook.ps1"
    }
    $candidates += @(
        Join-Path $HOME "miniconda3\shell\condabin\conda-hook.ps1"
        Join-Path $HOME "anaconda3\shell\condabin\conda-hook.ps1"
        "C:\ProgramData\miniconda3\shell\condabin\conda-hook.ps1"
        "C:\ProgramData\anaconda3\shell\condabin\conda-hook.ps1"
        "C:\miniconda3\shell\condabin\conda-hook.ps1"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            . $candidate
            conda activate $env:IMCODEX_CONDA_ENV
            return
        }
    }

    throw "IMCODEX_CONDA_ENV is set to '$env:IMCODEX_CONDA_ENV', but conda-hook.ps1 was not found. Set IMCODEX_PYTHON to an explicit Python path, or initialize conda for PowerShell."
}

function Resolve-ImcodexPython {
    if ($env:IMCODEX_PYTHON) {
        return $env:IMCODEX_PYTHON
    }

    $windowsVenvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $posixVenvPython = Join-Path $repoRoot ".venv/bin/python"
    if (Test-Path $windowsVenvPython) {
        return $windowsVenvPython
    }
    if (Test-Path $posixVenvPython) {
        return $posixVenvPython
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        return "python3"
    }
    return "python"
}

function Test-PortListening {
    param([int] $Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $asyncResult = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Wait-DedicatedCore {
    param([int] $Port)

    $timeout = if ($env:IMCODEX_CORE_START_TIMEOUT) { [int] $env:IMCODEX_CORE_START_TIMEOUT } else { 30 }
    for ($waited = 0; $waited -lt $timeout; $waited++) {
        if (Test-PortListening -Port $Port) {
            return
        }
        Start-Sleep -Seconds 1
    }

    throw "Dedicated core on $coreUrl did not become ready within ${timeout}s."
}

Import-DotEnv
Enable-CondaEnv

$python = Resolve-ImcodexPython
$coreMode = if ($env:IMCODEX_CORE_MODE) { $env:IMCODEX_CORE_MODE } else { "dedicated-ws" }
$coreUrl = if ($env:IMCODEX_CORE_URL) { $env:IMCODEX_CORE_URL } else { "" }
$corePort = if ($env:IMCODEX_CORE_PORT) { $env:IMCODEX_CORE_PORT } else { "" }
$appServerUrl = if ($env:IMCODEX_APP_SERVER_URL) { $env:IMCODEX_APP_SERVER_URL } else { "" }

if (-not $corePort -and $coreUrl -match "^ws://(127\.0\.0\.1|localhost):([0-9]+)$") {
    $corePort = $Matches[2]
}

if (-not $corePort) {
    $corePort = "8765"
}
if (-not $coreUrl) {
    $coreUrl = "ws://127.0.0.1:$corePort"
}

Write-Host "Starting imcodex from $repoRoot"
Write-Host "Using Python: $python"
if ($appServerUrl) {
    Write-Host "App Server target: $appServerUrl"
}
else {
    Write-Host "Legacy core mode: $coreMode"
}

if (-not $appServerUrl -and $coreMode -eq "dedicated-ws") {
    $env:IMCODEX_CORE_MODE = $coreMode
    $env:IMCODEX_CORE_URL = $coreUrl

    if (Test-PortListening -Port ([int] $corePort)) {
        Write-Host "Dedicated core already appears to be listening on $env:IMCODEX_CORE_URL"
    }
    else {
        Write-Host "Starting dedicated Codex core on $env:IMCODEX_CORE_URL"
        & $python -m imcodex core start --port $corePort
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        Wait-DedicatedCore -Port ([int] $corePort)
    }
}

& $python -m imcodex
exit $LASTEXITCODE
