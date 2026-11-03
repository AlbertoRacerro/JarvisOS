$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$FrontendRoot = Join-Path $RepoRoot "frontend"
$FrontendUrl = "http://localhost:5173"

function Show-NodeMissingMessage {
    Write-Host ""
    Write-Host "Node.js LTS with npm is required to start the JarvisOS frontend." -ForegroundColor Yellow
    Write-Host "Install it from the official Node.js website: https://nodejs.org/"
    Write-Host "After installation, reopen your terminal or double-click launcher and try again."
    Write-Host ""
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Show-NodeMissingMessage
    throw "node was not found on PATH."
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Show-NodeMissingMessage
    throw "npm was not found on PATH."
}

Set-Location $FrontendRoot

if (-not (Test-Path (Join-Path $FrontendRoot "node_modules"))) {
    npm install
}

Start-Job -Name "jarvisos-open-browser" -ScriptBlock {
    param($Url)
    Start-Sleep -Seconds 5
    Start-Process $Url
} -ArgumentList $FrontendUrl | Out-Null

npm run dev
