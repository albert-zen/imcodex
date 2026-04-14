Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = if ($env:IMCODEX_PYTHON) { $env:IMCODEX_PYTHON } else { "python" }
$minimumCodexVersion = [Version]"0.120.0"

function Write-Check([string]$Label, [bool]$Ok, [string]$Detail) {
    $status = if ($Ok) { "OK" } else { "FAIL" }
    Write-Host ("[{0}] {1}: {2}" -f $status, $Label, $Detail)
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

Write-Host "IMCodex doctor"
Write-Host "Repo: $repoRoot"
Write-Host ""

try {
    $pythonVersion = & $python -c "import sys; print(sys.version)"
    Write-Check "Python" $true $pythonVersion.Trim()
} catch {
    Write-Check "Python" $false $_.Exception.Message
    exit 1
}

try {
    & $python -c "import imcodex; print('import ok')" | Out-Null
    Write-Check "Python package" $true "imcodex import ok"
} catch {
    Write-Check "Python package" $false $_.Exception.Message
}

$codexBin = Get-Setting "IMCODEX_CODEX_BIN" "codex"
$codexCommand = Get-Command $codexBin -ErrorAction SilentlyContinue
Write-Check "Codex binary" ($null -ne $codexCommand) ($codexCommand.Path ?? "not found")

if ($null -ne $codexCommand) {
    try {
        $codexVersionText = (& $codexBin --version | Select-Object -First 1).Trim()
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
        Write-Check "Codex app-server" $true "app-server command is available"
    } catch {
        Write-Check "Codex app-server" $false $_.Exception.Message
    }
}

$envFileOk = Test-Path $dotenvPath
Write-Check ".env" $envFileOk ($dotenvPath)

$httpPort = [int](Get-Setting "IMCODEX_HTTP_PORT" "8000")
$appServerUrl = Get-Setting "IMCODEX_APP_SERVER_URL"
$dataDir = Get-Setting "IMCODEX_DATA_DIR" ".imcodex-data"

Write-Check "HTTP port" $true $httpPort
Write-Check "App-server URL" $true ($(if ($appServerUrl) { $appServerUrl } else { "not configured (shared-ws probe + stdio fallback)" }))
Write-Check "Data dir" $true $dataDir

$httpListeners = Get-NetTCPConnection -LocalPort $httpPort -ErrorAction SilentlyContinue

$httpPortDetail = if ($httpListeners) { "occupied" } else { "free" }
Write-Check "HTTP port free" ($null -eq $httpListeners) $httpPortDetail

if ($appServerUrl) {
    try {
        $uri = [Uri]$appServerUrl
        if ($uri.Scheme -in @("ws", "wss") -and $uri.Host -in @("127.0.0.1", "localhost") -and $uri.Port -gt 0) {
            $appListeners = Get-NetTCPConnection -LocalPort $uri.Port -ErrorAction SilentlyContinue
            $appPortDetail = if ($appListeners) { "listening" } else { "not listening" }
            Write-Check "Shared app-server listener" ($null -ne $appListeners) ("{0}:{1} {2}" -f $uri.Host, $uri.Port, $appPortDetail)
        } else {
            Write-Check "Shared app-server listener" $true "skipped non-local websocket check"
        }
    } catch {
        Write-Check "Shared app-server listener" $false ("invalid IMCODEX_APP_SERVER_URL: {0}" -f $appServerUrl)
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
