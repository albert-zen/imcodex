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
$appServerUrl = Get-Setting "IMCODEX_APP_SERVER_URL"
$coreMode = Get-Setting "IMCODEX_CORE_MODE" "dedicated-ws"
$coreUrl = Get-Setting "IMCODEX_CORE_URL"
$corePort = Get-Setting "IMCODEX_CORE_PORT"
$dataDir = Get-Setting "IMCODEX_DATA_DIR" ".imcodex"

if (-not $corePort -and $coreUrl -match "^ws://(127\.0\.0\.1|localhost):([0-9]+)$") {
    $corePort = $Matches[2]
}
if (-not $corePort) {
    $corePort = "8765"
}
if (-not $coreUrl) {
    $coreUrl = "ws://127.0.0.1:$corePort"
}

$supportedCoreModes = @("dedicated-ws", "shared-ws", "spawned-stdio")
Write-Check "Core mode" ($coreMode -in $supportedCoreModes) $coreMode

if ($coreMode -eq "dedicated-ws") {
    Write-Check "Core target" $true "$coreUrl (the launcher will start or reuse it)"
} elseif ($coreMode -eq "shared-ws") {
    $sharedCoreUrl = if ($appServerUrl) { $appServerUrl } else { $coreUrl }
    Write-Check "Core target" $true "$sharedCoreUrl (externally managed)"
} elseif ($coreMode -eq "spawned-stdio") {
    Write-Check "Core target" $true "stdio:// (bridge-managed)"
}

Write-Check "HTTP port" $true $httpPort
Write-Check "Data dir" $true $dataDir

$httpListeners = Get-NetTCPConnection -LocalPort $httpPort -ErrorAction SilentlyContinue

$httpPortDetail = if ($httpListeners) { "occupied" } else { "free" }
Write-Check "HTTP port free" ($null -eq $httpListeners) $httpPortDetail

if ($coreMode -eq "shared-ws") {
    $sharedCoreUrl = if ($appServerUrl) { $appServerUrl } else { $coreUrl }
    try {
        $uri = [Uri]$sharedCoreUrl
        if ($uri.Scheme -in @("ws", "wss") -and $uri.Host -in @("127.0.0.1", "localhost") -and $uri.Port -gt 0) {
            $appListeners = Get-NetTCPConnection -LocalPort $uri.Port -ErrorAction SilentlyContinue
            $appPortDetail = if ($appListeners) { "listening" } else { "not listening" }
            Write-Check "Shared app-server listener" ($null -ne $appListeners) ("{0}:{1} {2}" -f $uri.Host, $uri.Port, $appPortDetail)
        } else {
            Write-Check "Shared app-server listener" $true "skipped non-local websocket check"
        }
    } catch {
        Write-Check "Shared app-server listener" $false ("invalid shared core URL: {0}" -f $sharedCoreUrl)
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
