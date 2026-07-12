Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$minimumCodexVersion = [Version]"0.120.0"
$script:hasFailures = $false

function Write-Check([string]$Label, [bool]$Ok, [string]$Detail) {
    $status = if ($Ok) { "OK" } else { "FAIL" }
    Write-Host ("[{0}] {1}: {2}" -f $status, $Label, $Detail)
    if (-not $Ok) {
        $script:hasFailures = $true
    }
}

$dotenvPath = Join-Path $repoRoot ".env"
$dotenv = @{}
if (Test-Path $dotenvPath) {
    Get-Content $dotenvPath | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $parts = $line.Split("=", 2)
        $dotenv[$parts[0].Trim()] = $parts[1].Trim().Trim("'").Trim('"')
    }
}

function Get-Setting([string]$Name, [string]$Default = "") {
    if (Test-Path "Env:$Name") {
        return (Get-Item "Env:$Name").Value
    }
    if ($dotenv.ContainsKey($Name)) {
        return $dotenv[$Name]
    }
    return $Default
}

function Test-PortListening([string]$HostName, [int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
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

function Test-PortAvailable([int]$Port) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function Resolve-ImcodexPython {
    $configuredPython = Get-Setting "IMCODEX_PYTHON"
    if ($configuredPython) {
        return $configuredPython
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

$python = Resolve-ImcodexPython

Write-Host "IMCodex doctor"
Write-Host "Repo: $repoRoot"
Write-Host "Python: $python"
Write-Host ""

try {
    $pythonVersion = & $python -c "import sys; print(sys.version)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python exited with code $LASTEXITCODE"
    }
    Write-Check "Python" $true (($pythonVersion -join " ").Trim())
} catch {
    Write-Check "Python" $false $_.Exception.Message
    exit 1
}

try {
    & $python -c "import imcodex; print('import ok')" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python package import exited with code $LASTEXITCODE"
    }
    Write-Check "Python package" $true "imcodex import ok"
} catch {
    Write-Check "Python package" $false $_.Exception.Message
}

$codexBin = Get-Setting "IMCODEX_CODEX_BIN" "codex"
$codexCommand = Get-Command $codexBin -ErrorAction SilentlyContinue
$codexCommandDetail = if ($null -ne $codexCommand -and $codexCommand.Path) {
    $codexCommand.Path
} else {
    "not found"
}
Write-Check "Codex binary" ($null -ne $codexCommand) $codexCommandDetail

if ($null -ne $codexCommand) {
    try {
        $codexVersionText = (& $codexBin --version | Select-Object -First 1).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Codex version command exited with code $LASTEXITCODE"
        }
        $codexVersionMatch = [regex]::Match($codexVersionText, '(\d+\.\d+\.\d+)')
        if ($codexVersionMatch.Success) {
            $codexVersion = [Version]$codexVersionMatch.Groups[1].Value
            $codexOk = $codexVersion -ge $minimumCodexVersion
            $codexDetail = "$codexVersionText (need >= $minimumCodexVersion)"
            Write-Check "Codex version" $codexOk $codexDetail
        } else {
            Write-Check "Codex version" $false ("unable to parse version from '{0}'" -f $codexVersionText)
        }
    } catch {
        Write-Check "Codex version" $false $_.Exception.Message
    }

    try {
        & $codexBin app-server --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Codex app-server help exited with code $LASTEXITCODE"
        }
        Write-Check "Codex app-server" $true "app-server command is available"
    } catch {
        Write-Check "Codex app-server" $false $_.Exception.Message
    }
}

$envFileOk = Test-Path $dotenvPath
Write-Check ".env" $envFileOk ($dotenvPath)

$httpPort = [int](Get-Setting "IMCODEX_HTTP_PORT" "8000")
$dataDir = Get-Setting "IMCODEX_DATA_DIR" ".imcodex"
$targetConfigured = $false
foreach ($targetName in @(
    "IMCODEX_APP_SERVER_URL",
    "IMCODEX_CORE_URL",
    "IMCODEX_CORE_MODE",
    "IMCODEX_CORE_PORT"
)) {
    if (-not [string]::IsNullOrWhiteSpace((Get-Setting $targetName))) {
        $targetConfigured = $true
        break
    }
}

Write-Check "HTTP port" $true $httpPort
Write-Check "Data dir" $true $dataDir
$condaEnvName = Get-Setting "IMCODEX_CONDA_ENV"
if (-not [string]::IsNullOrWhiteSpace($condaEnvName)) {
    Write-Check "Conda target preview" $true "doctor does not activate conda; run it inside '$condaEnvName' to inspect target variables injected by that environment"
}

$targetOutput = @(& $python -c "import json; from imcodex.config import load_app_server_target; t = load_app_server_target(); print(json.dumps({'endpoint': t.endpoint, 'ownership': t.ownership, 'transport': t.transport}))" 2>&1)
$target = $null
if ($LASTEXITCODE -ne 0) {
    Write-Check "App-server target" $false (($targetOutput -join " ").Trim())
}
else {
    try {
        $target = ($targetOutput -join "") | ConvertFrom-Json
        Write-Check "App-server target" $true ("{0} ({1}, {2})" -f $target.endpoint, $target.ownership, $target.transport)
    }
    catch {
        Write-Check "App-server target" $false ("invalid target resolver output: {0}" -f ($targetOutput -join " "))
    }
}

$httpPortAvailable = Test-PortAvailable -Port $httpPort
$httpPortDetail = if ($httpPortAvailable) { "free" } else { "occupied" }
Write-Check "HTTP port free" $httpPortAvailable $httpPortDetail

if ($null -ne $target) {
    if ($target.transport -eq "tcp-websocket") {
        try {
            $uri = [Uri]$target.endpoint
            if ($uri.Host -in @("127.0.0.1", "localhost") -and $uri.Port -gt 0) {
                $appListening = Test-PortListening -HostName $uri.Host -Port $uri.Port
                if ($appListening) {
                    Write-Check "App-server listener" $true ("{0}:{1} listening" -f $uri.Host, $uri.Port)
                }
                elseif (-not $targetConfigured) {
                    Write-Check "App-server listener" $true ("{0}:{1} will be started by scripts/start.ps1" -f $uri.Host, $uri.Port)
                }
                else {
                    Write-Check "App-server listener" $false ("{0}:{1} not listening (explicit targets are connect-only)" -f $uri.Host, $uri.Port)
                }
            }
            else {
                Write-Check "App-server listener" $true "skipped non-local TCP WebSocket check"
            }
        }
        catch {
            Write-Check "App-server listener" $false ("invalid TCP WebSocket endpoint: {0}" -f $target.endpoint)
        }
    }
    elseif ($target.transport -eq "unix-websocket") {
        if ($env:OS -eq "Windows_NT") {
            Write-Check "App-server listener" $false "Unix control sockets require WSL/macOS/Linux; configure stdio:// or an explicit ws:// endpoint"
        }
        else {
            $rawSocketPath = $target.endpoint.Substring("unix://".Length)
            if (-not $rawSocketPath) {
                $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
                $socketPath = Join-Path $codexHome "app-server-control/app-server-control.sock"
            }
            elseif ([IO.Path]::IsPathRooted($rawSocketPath)) {
                $socketPath = $rawSocketPath
            }
            else {
                $socketPath = Join-Path $repoRoot $rawSocketPath
            }
            $socketExists = Test-Path $socketPath
            if ($socketExists) {
                Write-Check "App-server listener" $true ("Unix socket exists at {0}" -f $socketPath)
            }
            elseif (-not $targetConfigured) {
                Write-Check "App-server listener" $true ("Unix socket will be ensured by scripts/start.sh at {0}" -f $socketPath)
            }
            else {
                Write-Check "App-server listener" $false ("Unix socket is not available at {0} (explicit targets are connect-only)" -f $socketPath)
            }
        }
    }
    elseif ($target.transport -eq "stdio-jsonl") {
        Write-Check "App-server listener" $true "bridge-child App Server will start with the bridge"
    }
}

$qqEnabled = (Get-Setting "IMCODEX_QQ_ENABLED" "0").ToLower() -in @("1", "true", "yes", "on")
Write-Check "QQ enabled" $true $qqEnabled

if ($qqEnabled) {
    $qqAppId = Get-Setting "IMCODEX_QQ_APP_ID"
    $qqSecret = Get-Setting "IMCODEX_QQ_CLIENT_SECRET"
    $qqApiBase = Get-Setting "IMCODEX_QQ_API_BASE" "https://api.sgroup.qq.com"
    $qqAppIdDetail = if ($qqAppId) { "configured" } else { "missing" }
    $qqSecretDetail = if ($qqSecret) { "configured" } else { "missing" }
    Write-Check "QQ app id" (-not [string]::IsNullOrWhiteSpace($qqAppId)) $qqAppIdDetail
    Write-Check "QQ client secret" (-not [string]::IsNullOrWhiteSpace($qqSecret)) $qqSecretDetail
    Write-Check "QQ API base" $true $qqApiBase
}

Write-Host ""
try {
    & $python -m imcodex channels doctor
    $channelsOk = $LASTEXITCODE -eq 0
    Write-Check "Channel configuration" $channelsOk ($(if ($channelsOk) { "ready" } else { "see channel doctor output above" }))
    if (-not $channelsOk) {
        exit 1
    }
} catch {
    Write-Check "Channel configuration" $false $_.Exception.Message
    exit 1
}

if ($script:hasFailures) {
    exit 1
}
exit 0
