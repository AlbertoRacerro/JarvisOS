$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$BackendRoot = Join-Path $RepoRoot "backend"
$VenvRoot = Join-Path $BackendRoot ".venv"
$PythonExe = Join-Path $VenvRoot "Scripts\python.exe"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "Python 3.11 or newer is required to start the JarvisOS backend." -ForegroundColor Yellow
    Write-Host "Install Python, reopen your terminal or double-click launcher, and try again."
    Write-Host ""
    throw "python was not found on PATH."
}

Set-Location $BackendRoot

if (-not (Test-Path $PythonExe)) {
    python -m venv $VenvRoot
}

if (-not (Test-Path $PythonExe)) {
    Write-Host ""
    Write-Host "The backend virtual environment could not be created or used." -ForegroundColor Yellow
    Write-Host "Expected Python executable: $PythonExe"
    Write-Host ""
    throw "Backend virtual environment was not created correctly."
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $BackendRoot "requirements.txt")

$env:PYTHONPATH = $BackendRoot
& $PythonExe -m app.core.bootstrap
& $PythonExe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
