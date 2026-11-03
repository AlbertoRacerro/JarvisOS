$ErrorActionPreference = "Stop"

$BackendScript = Join-Path $PSScriptRoot "start-backend.ps1"
$FrontendScript = Join-Path $PSScriptRoot "start-frontend.ps1"

$jobs = @(
    Start-Job -Name "jarvisos-backend" -ScriptBlock { param($ScriptPath) & $ScriptPath } -ArgumentList $BackendScript
    Start-Job -Name "jarvisos-frontend" -ScriptBlock { param($ScriptPath) & $ScriptPath } -ArgumentList $FrontendScript
)

try {
    Write-Host "JarvisOS dev services are starting. Press Ctrl+C to stop."
    while ($true) {
        foreach ($job in $jobs) {
            Receive-Job -Job $job
            if ($job.State -in @("Failed", "Stopped", "Completed")) {
                throw "Dev service '$($job.Name)' exited with state $($job.State)."
            }
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    $jobs | Stop-Job
    $jobs | Remove-Job
}
