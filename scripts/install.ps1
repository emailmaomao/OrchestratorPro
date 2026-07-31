<#
.SYNOPSIS
    Install OrchestratorPro on Windows.

.DESCRIPTION
    Creates a virtual environment and installs the package into it, rather than
    into the Python the machine uses for everything else. OrchestratorPro pins a
    FastAPI range; an installer that changes the system Python is one that
    eventually breaks something unrelated and gets blamed for it.

.PARAMETER Prefix
    Where to install. Defaults to the repository root.

.PARAMETER Venv
    Virtual environment location. Defaults to <Prefix>\.venv.

.PARAMETER Dev
    Also install the test and lint tooling.

.PARAMETER Quiet
    Print less.

.EXAMPLE
    .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -Dev -Prefix C:\opt\orchestratorpro
#>
[CmdletBinding()]
param(
    [string]$Prefix,
    [string]$Venv,
    [switch]$Dev,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$MinMajor = 3
$MinMinor = 11

function Write-Step {
    param([string]$Message)
    if (-not $Quiet) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
}

function Write-Detail {
    param([string]$Message)
    if (-not $Quiet) { Write-Host "  $Message" }
}

function Stop-WithError {
    param([string]$Message)
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

if (-not $Prefix) {
    $Prefix = Split-Path -Parent $PSScriptRoot
}
if (-not $Venv) {
    $Venv = Join-Path $Prefix '.venv'
}

Write-Step 'Checking Python'

$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
}
if (-not $python) {
    Stop-WithError "no Python found; install Python $MinMajor.$MinMinor or newer from python.org"
}

# Ask the interpreter rather than parsing --version: that output format has
# changed before and will again.
& $python -c "import sys; raise SystemExit(0 if sys.version_info >= ($MinMajor, $MinMinor) else 1)"
if ($LASTEXITCODE -ne 0) {
    $version = (& $python --version 2>&1)
    Stop-WithError "$version is too old; $MinMajor.$MinMinor or newer is required"
}
Write-Detail "using $(& $python --version 2>&1) at $python"

Write-Step 'Checking git'
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    Write-Detail (& git --version)
} else {
    # A warning, not a failure: the control plane records, plans, and reports
    # without git. It just cannot give an agent a worktree.
    Write-Host '  WARNING: git is not installed; runs that need a worktree will fail' -ForegroundColor Yellow
}

Write-Step 'Creating the virtual environment'
if (Test-Path $Venv) {
    Write-Detail "reusing $Venv"
} else {
    & $python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Stop-WithError "could not create a virtual environment at $Venv" }
    Write-Detail "created $Venv"
}

$venvPython = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Stop-WithError "$Venv does not look like a virtual environment"
}

Write-Step 'Installing'
& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Stop-WithError 'could not upgrade pip' }

if ($Dev) {
    & $venvPython -m pip install --quiet -e "$Prefix[dev]"
    $what = 'installed with the development extras'
} else {
    & $venvPython -m pip install --quiet $Prefix
    $what = 'installed'
}
if ($LASTEXITCODE -ne 0) { Stop-WithError 'installation failed' }
Write-Detail $what

Write-Step 'Verifying'
$reported = & $venvPython -m orchestrator.cli version
if ($LASTEXITCODE -ne 0) { Stop-WithError 'the installed package does not run' }
Write-Detail $reported

$envFile = Join-Path $Prefix '.env'
$envExample = Join-Path $Prefix '.env.example'
if ((-not (Test-Path $envFile)) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    Write-Detail "wrote $envFile from the example - read it before serving"
}

Write-Step 'Done'
if (-not $Quiet) {
    Write-Host ''
    Write-Host "  $Venv\Scripts\orchestratorpro.exe config check"
    Write-Host "  $Venv\Scripts\orchestratorpro.exe serve"
    Write-Host ''
    Write-Host '  This build ships no authentication. It binds to 127.0.0.1 and refuses' -ForegroundColor Yellow
    Write-Host '  any other address unless you have configured allowed hosts and a token' -ForegroundColor Yellow
    Write-Host '  variable. Do not publish the port without an authenticating proxy.' -ForegroundColor Yellow
    Write-Host ''
}
