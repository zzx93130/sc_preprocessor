#!/usr/bin/env bash
# Build the three scpre images. Run once after installing Docker Desktop
# (enable "Use Rosetta for x86_64/amd64 emulation" in Docker Desktop settings).
#
# Dockerfiles are piped via stdin with NO build context: nothing is copied
# into the images, and this sidesteps macOS "._*" AppleDouble files on
# external drives that break context transfer ("failed to xattr ._...").
set -euo pipefail
cd "$(dirname "$0")"

echo ">> scpre-crbase (x86_64 base for Cell Ranger)"
docker build --platform linux/amd64 -t scpre-crbase:latest - < crbase.Dockerfile

echo ">> scpre-sratools (prefetch / fasterq-dump / pigz)"
docker build --platform linux/amd64 -t scpre-sratools:latest - < sratools.Dockerfile

echo ">> scpre-velocyto (x86_64 under Rosetta — native arm64 build hits a glibc libmvec gap)"
docker build --platform linux/amd64 -t scpre-velocyto:latest - < velocyto.Dockerfile

echo
echo "All images built:"
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep '^scpre-'
