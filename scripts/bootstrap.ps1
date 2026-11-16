<#
.SYNOPSIS
    One-command setup. Creates a virtual environment, installs the server,
    installs the credential-blocking git hook, and reports what is still
    missing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "mcp-cloudtube setup" -ForegroundColor Cyan
Write-Host ("-" * 40)

# --- Python ---------------------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python is not on PATH. Install Python 3.11 or newer." }

$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]'3.11') {
    throw "Python $version found, but 3.11 or newer is required."
}
Write-Host "python          $version"

# --- Virtual environment ----------------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "venv            creating..."
    & python -m venv .venv
} else {
    Write-Host "venv            already present"
}
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

# --- Dependencies -------------------------------------------------------------
Write-Host "dependencies    installing..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
Write-Host "dependencies    OK"

# --- Git hook -------------------------------------------------------------------
if (Test-Path ".git") {
    & git config core.hooksPath .githooks
    Write-Host "git hook        installed (blocks credential commits)"
}

# --- Config ---------------------------------------------------------------------
if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "config          .env created from .env.example"
}
New-Item -ItemType Directory -Force -Path ".secrets" | Out-Null

# --- Tests ------------------------------------------------------------------------
if (-not $SkipTests) {
    Write-Host "tests           running..."
    & $venvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed. The install is not healthy." }
}

# --- What is still needed -----------------------------------------------------------
Write-Host ""
& $venvPython -m mcp_cloudtube --check
Write-Host ""

if (-not (Test-Path ".secrets\client_secret.json")) {
    Write-Host "NEXT STEP" -ForegroundColor Yellow
    Write-Host "  Work through docs\SETUP_GOOGLE_CLOUD.md to create a Google Cloud"
    Write-Host "  project and download the OAuth client file to:"
    Write-Host "    .secrets\client_secret.json"
    Write-Host ""
    Write-Host "  Then connect a client: docs\CONNECTING_CLIENTS.md"
} else {
    Write-Host "Ready. Connect a client: docs\CONNECTING_CLIENTS.md" -ForegroundColor Green
}
