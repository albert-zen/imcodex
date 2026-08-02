$ErrorActionPreference = "Stop"

$smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("imcodex-sdk-smoke-" + [guid]::NewGuid().ToString("N"))
$dataRoot = Join-Path $smokeRoot "data"
$runRoot = Join-Path $smokeRoot "run"
$mediaRoot = Join-Path $dataRoot "channels\webhook\inbound-media"
$process = $null

try {
    New-Item -ItemType Directory -Force -Path $dataRoot, $runRoot, $mediaRoot | Out-Null
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()

    $env:IMCODEX_DATA_DIR = $dataRoot
    $env:IMCODEX_RUN_DIR = $runRoot
    $env:IMCODEX_HTTP_HOST = "127.0.0.1"
    $env:IMCODEX_HTTP_PORT = [string]$port
    $env:IMCODEX_APP_SERVER_URL = "stdio://"
    $env:IMCODEX_QQ_ENABLED = "0"
    $env:IMCODEX_TELEGRAM_ENABLED = "0"
    $env:IMCODEX_FEISHU_ENABLED = "0"
    $env:IMCODEX_WEIXIN_ENABLED = "0"
    $env:IMCODEX_DEBUG_API_ENABLED = "0"

    $stdout = Join-Path $smokeRoot "stdout.log"
    $stderr = Join-Path $smokeRoot "stderr.log"
    $python = (Get-Command python).Source
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "imcodex" `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    $health = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline -and -not $process.HasExited) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 1
            break
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if ($null -eq $health) {
        $detail = ((Get-Content $stderr -ErrorAction SilentlyContinue) -join "`n")
        throw "IMCodex Windows SDK startup failed. $detail"
    }

    $headers = @{ "x-imcodex-instance" = [string]$health.instanceId }
    $shutdown = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:$port/_imcodex/ops/shutdown" `
        -Headers $headers `
        -TimeoutSec 5
    if ($shutdown.status -ne "shutting_down") {
        throw "Graceful shutdown did not acknowledge the request."
    }
    $process.WaitForExit(15000)
    if (-not $process.HasExited -or $process.ExitCode -ne 0) {
        throw "IMCodex Windows SDK shutdown failed."
    }

    $snapshot = Get-Content (Join-Path $runRoot "current\health.json") -Raw | ConvertFrom-Json
    if ($snapshot.status -ne "stopped") {
        throw "Final health status is not stopped."
    }
    if ($snapshot.sdk.applications.PSObject.Properties.Name -notcontains "codex-main") {
        throw "SDK Codex Application diagnostics are missing."
    }
    if ($snapshot.sdk.channels.PSObject.Properties.Name -notcontains "webhook") {
        throw "SDK webhook Channel diagnostics are missing."
    }
    Write-Host "Windows SDK startup/shutdown smoke passed."
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit(5000)
    }
    if (Test-Path -LiteralPath $smokeRoot) {
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
}
