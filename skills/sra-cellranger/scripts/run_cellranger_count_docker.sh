#!/usr/bin/env bash
# Run Cell Ranger count inside a linux/amd64 Docker container.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <SAMPLE_ID>" >&2
  exit 2
fi

SAMPLE_ID="$1"
WORK="${WORK:-${PWD}/cellranger_work}"
CR_BIN_DIR="${CR_BIN_DIR:-${WORK}/cellranger/cellranger-10.0.0}"
CR_REF_DIR="${CR_REF_DIR:-${WORK}/reference/refdata-gex-GRCh38-2024-A}"
CR_FASTQ_BASE="${CR_FASTQ_BASE:-${WORK}/fastq_input}"
CR_OUTPUT_BASE="${CR_OUTPUT_BASE:-${WORK}/outputs}"
CR_RESULTS_BASE="${CR_RESULTS_BASE:-${WORK}/results}"
CR_LOG_DIR="${CR_LOG_DIR:-${WORK}/logs}"
CR_DOCKER_CONTEXT="${CR_DOCKER_CONTEXT:-desktop-linux}"
CR_DOCKER_IMAGE="${CR_DOCKER_IMAGE:-debian:bookworm-slim}"
CR_LOCALCORES="${CR_LOCALCORES:-8}"
CR_LOCALMEM_GB="${CR_LOCALMEM_GB:-24}"
CR_CHEMISTRY="${CR_CHEMISTRY:-auto}"
CR_INCLUDE_INTRONS="${CR_INCLUDE_INTRONS:-true}"
CR_VALIDATE_DOCKER="${CR_VALIDATE_DOCKER:-1}"
CR_WARN_DOCKER_MEM_GB="${CR_WARN_DOCKER_MEM_GB:-32}"
CR_COPY_MODE="${CR_COPY_MODE:-compact}"

FASTQ_DIR="${CR_FASTQ_BASE}/${SAMPLE_ID}"
RUN_DIR="${CR_OUTPUT_BASE}/${SAMPLE_ID}"
RESULT_DIR="${CR_RESULTS_BASE}/${SAMPLE_ID}/outs"
LOG="${CR_LOG_DIR}/cellranger_${SAMPLE_ID}.log"

if [[ ! -d "$CR_BIN_DIR" ]]; then echo "[err] missing Cell Ranger: $CR_BIN_DIR" >&2; exit 1; fi
if [[ ! -d "$CR_REF_DIR" ]]; then echo "[err] missing reference: $CR_REF_DIR" >&2; exit 1; fi
if [[ ! -d "$FASTQ_DIR" ]]; then echo "[err] missing staged FASTQs: $FASTQ_DIR" >&2; exit 1; fi

mkdir -p "$CR_OUTPUT_BASE" "$CR_RESULTS_BASE" "$CR_LOG_DIR"

FASTQ_COUNT="$(find "$FASTQ_DIR" -maxdepth 1 -name "${SAMPLE_ID}_S1_L*_R[12]_001.fastq.gz" | wc -l | tr -d ' ')"
if [[ "$FASTQ_COUNT" -lt 2 ]]; then
  echo "[err] no Cell Ranger-style FASTQ pairs found in $FASTQ_DIR" >&2
  exit 1
fi

if [[ "$CR_CHEMISTRY" == "auto" || "$CR_CHEMISTRY" == "AUTO" ]]; then
  first_r1="$(find "$FASTQ_DIR" -maxdepth 1 -name "${SAMPLE_ID}_S1_L*_R1_001.fastq.gz" | sort | head -1)"
  read_len="$(gzip -dc "$first_r1" | sed -n '2p' | awk '{ print length($0) }')"
  case "$read_len" in
    26) CR_CHEMISTRY="SC3Pv2" ;;
    28) CR_CHEMISTRY="SC3Pv3" ;;
    *) echo "[err] cannot auto-detect chemistry from R1 length=${read_len}; set CR_CHEMISTRY" >&2; exit 1 ;;
  esac
fi

source_roots() {
  python3 - "$FASTQ_DIR" <<'PY'
import os
import pathlib
import sys
for root in sorted({
    str(pathlib.Path(os.path.realpath(path)).parent) if path.is_symlink() else str(path.parent)
    for path in pathlib.Path(sys.argv[1]).iterdir()
    if path.name.endswith(".fastq.gz")
}):
    print(root)
PY
}

echo "[info] sample=${SAMPLE_ID} fastq_count=${FASTQ_COUNT} chemistry=${CR_CHEMISTRY}" | tee -a "$LOG"

if [[ "$CR_VALIDATE_DOCKER" == "1" ]]; then
  docker_mem_bytes="$(docker --context "$CR_DOCKER_CONTEXT" info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  docker_mem_gib="$(python3 - "$docker_mem_bytes" <<'PY'
import sys
try:
    print(f"{int(sys.argv[1]) / 1024**3:.1f}")
except Exception:
    print("0.0")
PY
)"
  echo "[info] docker_memory=${docker_mem_gib}GiB warn_threshold=${CR_WARN_DOCKER_MEM_GB}GiB" | tee -a "$LOG"
  docker --context "$CR_DOCKER_CONTEXT" run --rm \
    --platform linux/amd64 \
    -v "$CR_BIN_DIR":/opt/cellranger:ro \
    "$CR_DOCKER_IMAGE" \
    /opt/cellranger/bin/cellranger --version 2>&1 | tee -a "$LOG"
fi

DOCKER_MOUNTS=(
  -v "$CR_BIN_DIR":/opt/cellranger:ro
  -v "$CR_REF_DIR":/ref:ro
  -v "$FASTQ_DIR":"$FASTQ_DIR":ro
  -v "$CR_OUTPUT_BASE":/work
)
while IFS= read -r src_root; do
  [[ -n "$src_root" && "$src_root" != "$FASTQ_DIR" ]] || continue
  DOCKER_MOUNTS+=(-v "$src_root":"$src_root":ro)
done < <(source_roots)

cd "$CR_OUTPUT_BASE"
docker --context "$CR_DOCKER_CONTEXT" run --rm \
  --platform linux/amd64 \
  --cpus "$CR_LOCALCORES" \
  --memory "${CR_LOCALMEM_GB}g" \
  "${DOCKER_MOUNTS[@]}" \
  -w /work \
  -e PATH=/opt/cellranger/bin:/opt/cellranger/external/anaconda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  "$CR_DOCKER_IMAGE" \
  bash -lc "set -euo pipefail
    export PATH=/opt/cellranger/bin:/opt/cellranger/external/anaconda/bin:\$PATH
    rm -rf '${SAMPLE_ID}/_lock' '${SAMPLE_ID}/SC_RNA_COUNTER_CS' || true
    /opt/cellranger/bin/cellranger count \
      --id='${SAMPLE_ID}' \
      --transcriptome=/ref \
      --fastqs='${FASTQ_DIR}' \
      --sample='${SAMPLE_ID}' \
      --chemistry='${CR_CHEMISTRY}' \
      --create-bam=false \
      --include-introns=${CR_INCLUDE_INTRONS} \
      --localcores=${CR_LOCALCORES} \
      --localmem=${CR_LOCALMEM_GB} \
      --disable-ui" 2>&1 | tee -a "$LOG"

if [[ ! -d "${RUN_DIR}/outs" ]]; then
  echo "[err] Cell Ranger outs missing: ${RUN_DIR}/outs" | tee -a "$LOG" >&2
  exit 1
fi

rm -rf "$RESULT_DIR"
mkdir -p "$RESULT_DIR"
if [[ "$CR_COPY_MODE" == "full" ]]; then
  rm -rf "$RESULT_DIR"
  mkdir -p "$(dirname "$RESULT_DIR")"
  cp -R "${RUN_DIR}/outs" "$RESULT_DIR"
else
  for item in raw_feature_bc_matrix filtered_feature_bc_matrix web_summary.html metrics_summary.csv molecule_info.h5; do
    src="${RUN_DIR}/outs/${item}"
    [[ -e "$src" ]] || continue
    cp -R "$src" "${RESULT_DIR}/${item}"
  done
fi
rm -rf "$RUN_DIR"
echo "[done] ${SAMPLE_ID} results=${RESULT_DIR}" | tee -a "$LOG"
