[CmdletBinding()]
param(
    [int]$DockerTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$python = Join-Path $repoRoot 'pipeline\.venv\Scripts\python.exe'
$dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'scpre'
$serviceLog = Join-Path $runtimeRoot 'logs\windows-service.log'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment is missing: $python. Run install-windows.ps1 first."
}

New-Item -ItemType Directory -Force -Path (Split-Path $serviceLog) | Out-Null

docker.exe info --format '{{.ServerVersion}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    if (-not (Test-Path -LiteralPath $dockerDesktop)) {
        throw 'Docker Desktop is not installed.'
    }
    Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($DockerTimeoutSeconds)
    do {
        Start-Sleep -Seconds 3
        docker.exe info --format '{{.ServerVersion}}' 2>$null | Out-Null
    } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Desktop did not become ready before the timeout.'
    }
}

Push-Location (Join-Path $repoRoot 'pipeline')
try {
    # Python's logging module writes ordinary INFO messages to stderr.  Windows
    # PowerShell converts redirected native stderr into ErrorRecord objects;
    # with Stop in effect, the first harmless log line terminates the launcher.
    # Keep strict error handling for setup, but allow the long-running native
    # service to stream both output channels into its log normally.
    $ErrorActionPreference = 'Continue'
    & $python -m scpre --root $repoRoot start 2>&1 |
        Tee-Object -FilePath $serviceLog -Append
    $serviceExitCode = $LASTEXITCODE
    exit $serviceExitCode
}
finally {
    Pop-Location
}
