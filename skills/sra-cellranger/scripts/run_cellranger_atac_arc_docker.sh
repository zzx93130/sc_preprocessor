#!/usr/bin/env bash
# Run cellranger-atac count (snATAC) or cellranger-arc count (Multiome snATAC + snRNA)
# inside a linux/amd64 Docker container. Companion to run_cellranger_count_docker.sh.
#
# Staged FASTQ layout under ${CR_FASTQ_BASE}/<SAMPLE_ID>/:
#   atac:      <SAMPLE_ID>_S1_L00n_{R1,R2,R3[,I1]}_001.fastq.gz
#   multiome:  gex/<SAMPLE_ID>_gex_S1_L00n_{R1,R2}_001.fastq.gz
#              atac/<SAMPLE_ID>_atac_S1_L00n_{R1,R2,R3[,I1]}_001.fastq.gz

set -euo pipefail

if [[ $# -lt 2 || ( "$1" != "atac" && "$1" != "multiome" ) ]]; then
  echo "usage: $0 <atac|multiome> <SAMPLE_ID>" >&2
  exit 2
fi

ASSAY="$1"
SAMPLE_ID="$2"
WORK="${WORK:-${PWD}/cellranger_work}"
if [[ "$ASSAY" == "atac" ]]; then
  TOOL="cellranger-atac"
  CR_BIN_DIR="${CR_ATAC_BIN_DIR:-${WORK}/cellranger/cellranger-atac-2.2.0}"
else
  TOOL="cellranger-arc"
  CR_BIN_DIR="${CR_ARC_BIN_DIR:-${WORK}/cellranger/cellranger-arc-2.2.0}"
fi
# cellranger-atac accepts either a Cell Ranger ATAC or a Cell Ranger ARC reference.
CR_REF_DIR="${CR_ATAC_REF_DIR:-${WORK}/reference/refdata-cellranger-arc-GRCh38-2020-A-2.0.0}"
CR_FASTQ_BASE="${CR_FASTQ_BASE:-${WORK}/fastq_input}"
CR_OUTPUT_BASE="${CR_OUTPUT_BASE:-${WORK}/outputs}"
CR_RESULTS_BASE="${CR_RESULTS_BASE:-${WORK}/results}"
CR_LOG_DIR="${CR_LOG_DIR:-${WORK}/logs}"
CR_DOCKER_CONTEXT="${CR_DOCKER_CONTEXT:-desktop-linux}"
CR_DOCKER_IMAGE="${CR_DOCKER_IMAGE:-debian:bookworm-slim}"
CR_LOCALCORES="${CR_LOCALCORES:-8}"
CR_LOCALMEM_GB="${CR_LOCALMEM_GB:-56}"
CR_VALIDATE_DOCKER="${CR_VALIDATE_DOCKER:-1}"
CR_WARN_DOCKER_MEM_GB="${CR_WARN_DOCKER_MEM_GB:-64}"
CR_COPY_MODE="${CR_COPY_MODE:-compact}"
# Cell Ranger ARC >= 2.1 requires --create-bam. For ARC 2.0 set CR_ARC_BAM_FLAG=--no-bam
# (or to an empty string to keep BAMs).
CR_ARC_BAM_FLAG="${CR_ARC_BAM_FLAG---create-bam=false}"
CR_EXTRA_ARGS="${CR_EXTRA_ARGS:-}"

FASTQ_DIR="${CR_FASTQ_BASE}/${SAMPLE_ID}"
RUN_DIR="${CR_OUTPUT_BASE}/${SAMPLE_ID}"
RESULT_DIR="${CR_RESULTS_BASE}/${SAMPLE_ID}/outs"
LOG="${CR_LOG_DIR}/${TOOL}_${SAMPLE_ID}.log"

if [[ ! -d "$CR_BIN_DIR" ]]; then echo "[err] missing ${TOOL}: $CR_BIN_DIR" >&2; exit 1; fi
if [[ ! -d "$CR_REF_DIR" ]]; then echo "[err] missing ATAC/ARC reference: $CR_REF_DIR" >&2; exit 1; fi
if [[ ! -d "$FASTQ_DIR" ]]; then echo "[err] missing staged FASTQs: $FASTQ_DIR" >&2; exit 1; fi

mkdir -p "$CR_OUTPUT_BASE" "$CR_RESULTS_BASE" "$CR_LOG_DIR"

count_role() { # <dir> <prefix> <role>
  find "$1" -maxdepth 1 -name "${2}_S1_L*_${3}_001.fastq.gz" | wc -l | tr -d ' '
}

first_read_len() { # <dir> <prefix> <role>
  local first
  first="$(find "$1" -maxdepth 1 -name "${2}_S1_L*_${3}_001.fastq.gz" | sort | head -1)"
  gzip -dc "$first" | sed -n '2p' | awk '{ print length($0) }' || true
}

check_atac() { # <dir> <prefix>
  local role n
  for role in R1 R2 R3; do
    n="$(count_role "$1" "$2" "$role")"
    if [[ "$n" -lt 1 ]]; then
      echo "[err] no ${2}_S1_L*_${role}_001.fastq.gz in $1 (ATAC needs R1, R2 = barcode, R3)" >&2
      exit 1
    fi
  done
  local bc_len
  bc_len="$(first_read_len "$1" "$2" R2)"
  echo "[info] ATAC barcode read (R2) length=${bc_len}" | tee -a "$LOG"
  if [[ -z "$bc_len" || "$bc_len" -lt 16 || "$bc_len" -gt 24 ]]; then
    echo "[err] ATAC R2 should be the 16 bp barcode read (24 bp for Multiome); got ${bc_len}. Check the read map." >&2
    exit 1
  fi
}

if [[ "$ASSAY" == "atac" ]]; then
  check_atac "$FASTQ_DIR" "$SAMPLE_ID"
else
  for sub in gex atac; do
    if [[ ! -d "${FASTQ_DIR}/${sub}" ]]; then echo "[err] missing ${FASTQ_DIR}/${sub}" >&2; exit 1; fi
  done
  for role in R1 R2; do
    if [[ "$(count_role "${FASTQ_DIR}/gex" "${SAMPLE_ID}_gex" "$role")" -lt 1 ]]; then
      echo "[err] no ${SAMPLE_ID}_gex_S1_L*_${role}_001.fastq.gz in ${FASTQ_DIR}/gex" >&2
      exit 1
    fi
  done
  check_atac "${FASTQ_DIR}/atac" "${SAMPLE_ID}_atac"
  LIBRARIES_CSV="${CR_OUTPUT_BASE}/${SAMPLE_ID}_libraries.csv"
  {
    echo "fastqs,sample,library_type"
    echo "${FASTQ_DIR}/gex,${SAMPLE_ID}_gex,Gene Expression"
    echo "${FASTQ_DIR}/atac,${SAMPLE_ID}_atac,Chromatin Accessibility"
  } > "$LIBRARIES_CSV"
  echo "[info] libraries_csv=${LIBRARIES_CSV}" | tee -a "$LOG"
fi

source_roots() {
  python3 - "$FASTQ_DIR" <<'PY'
import os
import pathlib
import sys
for root in sorted({
    str(pathlib.Path(os.path.realpath(path)).parent)
    for path in pathlib.Path(sys.argv[1]).rglob("*.fastq.gz")
    if path.is_symlink()
}):
    print(root)
PY
}

echo "[info] assay=${ASSAY} sample=${SAMPLE_ID} tool=${TOOL}" | tee -a "$LOG"

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
    -v "$CR_BIN_DIR":/opt/tenx:ro \
    "$CR_DOCKER_IMAGE" \
    "/opt/tenx/bin/${TOOL}" --version 2>&1 | tee -a "$LOG"
fi

DOCKER_MOUNTS=(
  -v "$CR_BIN_DIR":/opt/tenx:ro
  -v "$CR_REF_DIR":/ref:ro
  -v "$FASTQ_DIR":"$FASTQ_DIR":ro
  -v "$CR_OUTPUT_BASE":/work
)
while IFS= read -r src_root; do
  [[ -n "$src_root" && "$src_root" != "$FASTQ_DIR" ]] || continue
  DOCKER_MOUNTS+=(-v "$src_root":"$src_root":ro)
done < <(source_roots)

if [[ "$ASSAY" == "atac" ]]; then
  INPUT_ARGS="--fastqs='${FASTQ_DIR}' --sample='${SAMPLE_ID}'"
  STALE="SC_ATAC_COUNTER_CS"
else
  INPUT_ARGS="--libraries='/work/${SAMPLE_ID}_libraries.csv' ${CR_ARC_BAM_FLAG}"
  STALE="SC_ATAC_GEX_COUNTER_CS"
fi

cd "$CR_OUTPUT_BASE"
docker --context "$CR_DOCKER_CONTEXT" run --rm \
  --platform linux/amd64 \
  --cpus "$CR_LOCALCORES" \
  --memory "${CR_LOCALMEM_GB}g" \
  "${DOCKER_MOUNTS[@]}" \
  -w /work \
  -e PATH=/opt/tenx/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  "$CR_DOCKER_IMAGE" \
  bash -lc "set -euo pipefail
    export PATH=/opt/tenx/bin:\$PATH
    rm -rf '${SAMPLE_ID}/_lock' '${SAMPLE_ID}/${STALE}' || true
    /opt/tenx/bin/${TOOL} count \
      --id='${SAMPLE_ID}' \
      --reference=/ref \
      ${INPUT_ARGS} \
      --localcores=${CR_LOCALCORES} \
      --localmem=${CR_LOCALMEM_GB} \
      --disable-ui ${CR_EXTRA_ARGS}" 2>&1 | tee -a "$LOG"

if [[ ! -d "${RUN_DIR}/outs" ]]; then
  echo "[err] ${TOOL} outs missing: ${RUN_DIR}/outs" | tee -a "$LOG" >&2
  exit 1
fi

if [[ "$ASSAY" == "atac" ]]; then
  REQUIRED="fragments.tsv.gz"
  KEEP=(fragments.tsv.gz fragments.tsv.gz.tbi filtered_peak_bc_matrix filtered_peak_bc_matrix.h5
        raw_peak_bc_matrix.h5 peaks.bed peak_annotation.tsv singlecell.csv summary.csv summary.json web_summary.html)
else
  REQUIRED="atac_fragments.tsv.gz"
  KEEP=(filtered_feature_bc_matrix filtered_feature_bc_matrix.h5 raw_feature_bc_matrix.h5
        atac_fragments.tsv.gz atac_fragments.tsv.gz.tbi atac_peaks.bed atac_peak_annotation.tsv
        per_barcode_metrics.csv gex_molecule_info.h5 summary.csv web_summary.html)
fi
if [[ ! -s "${RUN_DIR}/outs/${REQUIRED}" ]]; then
  echo "[err] ${REQUIRED} missing or empty; keeping pipestance ${RUN_DIR}" | tee -a "$LOG" >&2
  exit 1
fi

rm -rf "$RESULT_DIR"
mkdir -p "$(dirname "$RESULT_DIR")"
if [[ "$CR_COPY_MODE" == "full" ]]; then
  cp -R "${RUN_DIR}/outs" "$RESULT_DIR"
else
  mkdir -p "$RESULT_DIR"
  for item in "${KEEP[@]}"; do
    src="${RUN_DIR}/outs/${item}"
    [[ -e "$src" ]] || continue
    cp -R "$src" "${RESULT_DIR}/${item}"
  done
fi
rm -rf "$RUN_DIR"
[[ "$ASSAY" == "multiome" ]] && rm -f "$LIBRARIES_CSV"
echo "[done] ${ASSAY} ${SAMPLE_ID} results=${RESULT_DIR}" | tee -a "$LOG"
