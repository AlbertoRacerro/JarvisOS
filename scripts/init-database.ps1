$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$BackendRoot = Join-Path $RepoRoot "backend"
$VenvRoot = Join-Path $BackendRoot ".venv"
$PythonExe = Join-Path $VenvRoot "Scripts\python.exe"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python was not found. Install Python 3.11 or newer, then run this script again."
}

Set-Location $BackendRoot

if (-not (Test-Path $PythonExe)) {
    python -m venv $VenvRoot
}

if (-not (Test-Path $PythonExe)) {
    throw "Backend virtual environment was not created correctly at $VenvRoot."
}

& $PythonExe -m pip install -r (Join-Path $BackendRoot "requirements.txt")

$env:PYTHONPATH = $BackendRoot
& $PythonExe -m app.core.bootstrap
