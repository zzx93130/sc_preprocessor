[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$CellRangerArchive,
    [Parameter(Mandatory=$true)][string]$ReferenceDir,
    [ValidateRange(1,256)][int]$Cores = 10,
    [ValidateRange(16,1024)][int]$MemoryGB = 36,
    [switch]$SkipImages
)
$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $repoRoot 'config.yaml')) {
    throw 'config.yaml already exists. Refusing to overwrite an existing deployment.'
}
if (-not (Test-Path -LiteralPath $CellRangerArchive -PathType Leaf)) { throw 'Cell Ranger Linux archive is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $ReferenceDir 'reference.json'))) { throw 'Reference must be an extracted 10x reference with reference.json.' }
$venvPython = Join-Path $repoRoot 'pipeline\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    py.exe -3 -m venv (Join-Path $repoRoot 'pipeline\.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ with the py launcher is required.' }
}
& $venvPython -m pip install -r (Join-Path $repoRoot 'pipeline\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
if (-not $SkipImages) {
    & (Join-Path $repoRoot 'pipeline\docker\build_images.ps1') -CellRangerArchive $CellRangerArchive
}
& $venvPython (Join-Path $repoRoot 'deploy\configure_windows.py') --root $repoRoot --data-root $DataRoot --reference $ReferenceDir --cores $Cores --memory-gb $MemoryGB
if ($LASTEXITCODE -ne 0) { throw 'Configuration failed.' }
Write-Host 'Installed, not started. Review config.yaml, place SraRunTable.csv in to_process, then run start-windows.ps1.'
