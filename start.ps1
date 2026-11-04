[CmdletBinding()]
param(
    [string]$ModelApiKey,
    [string]$ChatModel = "muse-spark-1.3",
    [int]$Port = 8002,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"
$envExample = Join-Path $root ".env.example"
$logDirectory = Join-Path $root "runtime_logs"
$serverLog = Join-Path $logDirectory "server.log"
$serverErrorLog = Join-Path $logDirectory "server-error.log"
$baseUrl = "http://127.0.0.1:$Port"

function Stop-WithError([string]$Message) {
    Write-Error $Message
    exit 1
}

Set-Location $root

if (-not (Test-Path $python)) {
    Write-Host "Creating Python virtual environment..."
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Could not create .venv. Install Python 3.12+ and try again."
    }
}

$requirementsHashPath = Join-Path $root ".venv\requirements.hash"
$currentHash = (Get-FileHash -Path (Join-Path $root "requirements.txt") -Algorithm SHA256).Hash
$cachedHash = if (Test-Path $requirementsHashPath) { (Get-Content $requirementsHashPath -Raw).Trim() } else { "" }
if ($cachedHash -eq $currentHash -and (Test-Path $requirementsHashPath)) {
    Write-Host "Dependencies up to date (requirements hash $currentHash)."
} else {
    Write-Host "Installing pinned dependencies..."
    & $python -m pip install -r requirements.txt --disable-pip-version-check -q
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Dependency installation failed."
    }
    Set-Content -Path $requirementsHashPath -Value $currentHash -NoNewline
}

if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $envExample)) {
        Stop-WithError "Missing .env.example."
    }
    Copy-Item $envExample $envFile
    Write-Host "Created .env from .env.example. Add MODEL_API_KEY and rerun this script."
}

$envLines = Get-Content $envFile
$configuredKey = $envLines |
    Where-Object { $_ -match "^\s*MODEL_API_KEY\s*=" } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($ModelApiKey) -and (
        $null -eq $configuredKey -or
        $configuredKey -match "=\s*$" -or
        $configuredKey -match "your_meta_model_api_key"
    )) {
    Stop-WithError "Set MODEL_API_KEY in .env before starting Voice-Pilot."
}

if (-not [string]::IsNullOrWhiteSpace($ModelApiKey)) {
    $updatedLines = @()
    $keyWritten = $false
    foreach ($line in $envLines) {
        if ($line -match "^\s*MODEL_API_KEY\s*=") {
            $updatedLines += "MODEL_API_KEY=$ModelApiKey"
            $keyWritten = $true
        } else {
            $updatedLines += $line
        }
    }
    if (-not $keyWritten) {
        $updatedLines += "MODEL_API_KEY=$ModelApiKey"
    }
    Set-Content -Path $envFile -Value $updatedLines -Encoding utf8
}

$currentModel = $envLines | Where-Object { $_ -match "^\s*MUSE_CHAT_MODEL\s*=" } | Select-Object -First 1
if ($null -eq $currentModel) {
    Add-Content -Path $envFile -Value "MUSE_CHAT_MODEL=$ChatModel"
} elseif ($ChatModel) {
    (Get-Content $envFile) -replace "^\s*MUSE_CHAT_MODEL\s*=.*$", "MUSE_CHAT_MODEL=$ChatModel" |
        Set-Content -Path $envFile -Encoding utf8
}

if (-not $SkipTests) {
    Write-Host "Running tests..."
    & $python -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Tests failed. The server was not started."
    }
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    try {
        $health = Invoke-WebRequest -UseBasicParsing "$baseUrl/api/health" -TimeoutSec 5
        if ($health.StatusCode -eq 200) {
            Write-Host "Port $Port is in use. Stopping the previous Voice-Pilot server first..."
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match "uvicorn" -and $_.CommandLine -match "app\.main" } |
                ForEach-Object {
                    try {
                        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                        Write-Host "Stopped previous server (PID $($_.ProcessId))."
                    } catch {
                        Write-Host "Could not stop PID $($_.ProcessId): $($_.Exception.Message)"
                    }
                }
            for ($wait = 1; $wait -le 15; $wait++) {
                Start-Sleep -Seconds 1
                $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
                if ($null -eq $busy) { break }
            }
        }
    } catch {
        Write-Host "Health check failed; stopping whatever holds the port."
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "uvicorn" -and $_.CommandLine -match "app\.main" } |
            ForEach-Object {
                try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
            }
        for ($wait = 1; $wait -le 15; $wait++) {
            Start-Sleep -Seconds 1
            $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if ($null -eq $busy) { break }
        }
    }
    $stillThere = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -ne $stillThere) {
        Stop-WithError "Port $Port is still occupied. Stop that process and rerun."
    }
    # Fall through to a fresh start below.
    if ($null -eq $stillThere) {
        Write-Host "Port $Port is free. Starting fresh..."
    }
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Write-Host "Starting Voice-Pilot on $baseUrl ..."
$server = Start-Process -FilePath $python `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port" `
    -WorkingDirectory $root `
    -RedirectStandardOutput $serverLog `
    -RedirectStandardError $serverErrorLog `
    -PassThru

$healthy = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    Start-Sleep -Seconds 1
    if ($server.HasExited) {
        Write-Host (Get-Content $serverErrorLog -Tail 30 -ErrorAction SilentlyContinue)
        Stop-WithError "Voice-Pilot stopped during startup. See $serverErrorLog."
    }
    try {
        $health = Invoke-WebRequest -UseBasicParsing "$baseUrl/api/health" -TimeoutSec 3
        if ($health.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    } catch {
    }
}

if (-not $healthy) {
    Stop-WithError "Voice-Pilot did not become healthy within 60 seconds. See $serverErrorLog."
}

$rootResponse = Invoke-WebRequest -UseBasicParsing "$baseUrl/" -TimeoutSec 5
$bundleResponse = Invoke-WebRequest -UseBasicParsing "$baseUrl/static/app.js" -TimeoutSec 5
if ($rootResponse.StatusCode -ne 200 -or $bundleResponse.StatusCode -ne 200) {
    Stop-WithError "Backend health passed, but the frontend endpoint check failed."
}

Write-Host ""
Write-Host "Voice-Pilot is ready."
Write-Host "URL: $baseUrl"
Write-Host "PID: $($server.Id)"
Write-Host "Health: $($health.Content)"
Write-Host "Logs: $serverLog"
Write-Host "Errors: $serverErrorLog"
