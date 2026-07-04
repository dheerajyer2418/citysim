# CitySim one-step launcher.
#
# Easiest use: double-click run.bat.
# Or from a PowerShell prompt in the repo root:
#     .\run.ps1                # start the scenario builder and open the browser
#     .\run.ps1 --port 8010    # any `cli.py serve` flags pass straight through
#
# This script prepares the Python environment (first run only), points Java at
# the bundled JDK so the model-run buttons work, then starts the local app.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "First-time setup: creating the Python 3.11 environment (this happens once)..." -ForegroundColor Cyan
    py -3.11 -m venv .venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r requirements.txt
}

$jdk = Join-Path $PSScriptRoot "tools\jdk-17.0.19+10"
if (Test-Path $jdk) {
    $env:JAVA_HOME = $jdk
    $env:PATH = "$jdk\bin;$env:PATH"
}

Write-Host "Starting CitySim... your browser will open automatically." -ForegroundColor Green
& $py cli.py serve @args
