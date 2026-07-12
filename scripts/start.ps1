Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$script:TargetEnvironmentKeys = @(
    "IMCODEX_APP_SERVER_URL",
    "IMCODEX_CORE_URL",
    "IMCODEX_CORE_MODE",
    "IMCODEX_CORE_PORT"
)
$script:PreActivationTargetValues = @{}
$script:PreActivationTargetConfigured = $false
foreach ($targetKey in $script:TargetEnvironmentKeys) {
    $targetValue = [Environment]::GetEnvironmentVariable($targetKey, "Process")
    $script:PreActivationTargetValues[$targetKey] = $targetValue
    if (-not [string]::IsNullOrWhiteSpace($targetValue)) {
        $script:PreActivationTargetConfigured = $true
    }
}
$script:DotEnvTargetValues = @{}

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
        if ($script:TargetEnvironmentKeys -contains $key) {
            $script:DotEnvTargetValues[$key] = $value
            continue
        }
        if ($null -eq [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

function Set-TargetEnvironmentValue([string] $Name, $Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        [Environment]::SetEnvironmentVariable($Name, $null, "Process")
    }
    else {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Resolve-TargetEnvironment {
    if ($script:PreActivationTargetConfigured) {
        foreach ($targetKey in $script:TargetEnvironmentKeys) {
            Set-TargetEnvironmentValue $targetKey $script:PreActivationTargetValues[$targetKey]
        }
        return
    }

    foreach ($targetKey in $script:TargetEnvironmentKeys) {
        $targetValue = [Environment]::GetEnvironmentVariable($targetKey, "Process")
        if (-not [string]::IsNullOrWhiteSpace($targetValue)) {
            return
        }
    }

    foreach ($targetKey in $script:TargetEnvironmentKeys) {
        $targetValue = if ($script:DotEnvTargetValues.ContainsKey($targetKey)) {
            $script:DotEnvTargetValues[$targetKey]
        }
        else {
            $null
        }
        Set-TargetEnvironmentValue $targetKey $targetValue
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
Resolve-TargetEnvironment

$python = Resolve-ImcodexPython
$coreMode = if ([string]::IsNullOrWhiteSpace($env:IMCODEX_CORE_MODE)) { "" } else { $env:IMCODEX_CORE_MODE.Trim() }
$coreUrl = if ([string]::IsNullOrWhiteSpace($env:IMCODEX_CORE_URL)) { "" } else { $env:IMCODEX_CORE_URL.Trim() }
$corePort = if ([string]::IsNullOrWhiteSpace($env:IMCODEX_CORE_PORT)) { "" } else { $env:IMCODEX_CORE_PORT.Trim() }
$appServerUrl = if ([string]::IsNullOrWhiteSpace($env:IMCODEX_APP_SERVER_URL)) { "" } else { $env:IMCODEX_APP_SERVER_URL.Trim() }
$legacyCoreConfigured = [bool]($coreMode -or $coreUrl -or $corePort)
$ensureDedicatedCore = $false

if (-not $coreMode) {
    $coreMode = "dedicated-ws"
}

if (-not $corePort -and $coreUrl -match "^ws://(127\.0\.0\.1|localhost):([0-9]+)$") {
    $corePort = $Matches[2]
}

if (-not $corePort) {
    $corePort = "8765"
}
if (-not $coreUrl) {
    $coreUrl = "ws://127.0.0.1:$corePort"
}

if (-not $appServerUrl -and -not $legacyCoreConfigured) {
    $appServerUrl = $coreUrl
    $env:IMCODEX_APP_SERVER_URL = $appServerUrl
    $ensureDedicatedCore = $true
}

Write-Host "Starting imcodex from $repoRoot"
Write-Host "Using Python: $python"
if ($appServerUrl) {
    Write-Host "App Server target: $appServerUrl"
}
else {
    Write-Host "Legacy core mode: $coreMode"
}

if ($ensureDedicatedCore -or (-not $appServerUrl -and $legacyCoreConfigured -and $coreMode -eq "dedicated-ws")) {
    if (-not $ensureDedicatedCore) {
        $env:IMCODEX_CORE_MODE = $coreMode
        $env:IMCODEX_CORE_URL = $coreUrl
    }

    if (Test-PortListening -Port ([int] $corePort)) {
        Write-Host "Dedicated App Server already appears to be listening on $coreUrl"
    }
    else {
        Write-Host "Starting dedicated Codex App Server on $coreUrl"
        & $python -m imcodex core start --port $corePort
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        Wait-DedicatedCore -Port ([int] $corePort)
    }
}

& $python -m imcodex
exit $LASTEXITCODE
