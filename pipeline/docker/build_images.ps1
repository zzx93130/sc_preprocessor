[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CellRangerArchive
)

$ErrorActionPreference = 'Stop'
$dockerDir = $PSScriptRoot
$repoRoot = Split-Path (Split-Path $dockerDir)
$cacheDir = Join-Path $repoRoot 'runs\cache'
$sraArchive = Join-Path $cacheDir 'sratoolkit.3.2.1-ubuntu64.tar.gz'
$sraUrl = 'https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/3.2.1/sratoolkit.3.2.1-ubuntu64.tar.gz'

docker.exe info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is not ready.'
}

Write-Host '>> scpre-crbase:latest'
docker.exe image inspect ubuntu:24.04 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    docker.exe pull ubuntu:24.04
    if ($LASTEXITCODE -ne 0) { throw 'Failed to pull Ubuntu image.' }
}
docker.exe tag ubuntu:24.04 scpre-crbase:latest
if ($LASTEXITCODE -ne 0) { throw 'Failed to tag Cell Ranger base image.' }

Write-Host '>> scpre-sratools:latest'
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
if (-not (Test-Path -LiteralPath $sraArchive)) {
    Invoke-WebRequest -Uri $sraUrl -OutFile $sraArchive
}
docker.exe build --platform linux/amd64 --tag scpre-sratools:latest `
    --file (Join-Path $dockerDir 'sratools.windows.Dockerfile') $cacheDir
if ($LASTEXITCODE -ne 0) {
    throw 'Docker image build failed: scpre-sratools:latest'
}

Write-Host '>> scpre-velocyto:latest'
docker.exe image inspect varunkas/velocyto:0.17 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    docker.exe pull varunkas/velocyto:0.17
    if ($LASTEXITCODE -ne 0) { throw 'Failed to pull velocyto image.' }
}
docker.exe tag varunkas/velocyto:0.17 scpre-velocyto:latest
if ($LASTEXITCODE -ne 0) { throw 'Failed to tag velocyto image.' }

Write-Host '>> scpre-cellranger-10.1.0 volume'
$cellrangerTar = (Resolve-Path -LiteralPath $CellRangerArchive).Path
if (-not (Test-Path -LiteralPath $cellrangerTar)) {
    throw "Cell Ranger archive not found: $cellrangerTar"
}
docker.exe volume inspect scpre-cellranger-10.1.0 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    docker.exe volume create scpre-cellranger-10.1.0 | Out-Null
}
docker.exe run --rm `
    --mount "type=bind,source=$(Split-Path $cellrangerTar),target=/source,readonly" `
    --mount 'type=volume,source=scpre-cellranger-10.1.0,target=/opt/cellranger' `
    -e "ARCHIVE_NAME=$(Split-Path $cellrangerTar -Leaf)" `
    ubuntu:24.04 sh -ec `
    'if [ ! -x /opt/cellranger/cellranger ]; then if [ -n "$(ls -A /opt/cellranger)" ]; then echo "Refusing to overwrite nonempty Cell Ranger volume" >&2; exit 1; fi; tar -xzf "/source/$ARCHIVE_NAME" --strip-components=1 -C /opt/cellranger; fi; /opt/cellranger/cellranger --version'
if ($LASTEXITCODE -ne 0) {
    throw 'Cell Ranger Docker volume preparation failed.'
}

Write-Host 'Windows workflow images are ready:'
docker.exe images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' |
    Select-String '^scpre-'
