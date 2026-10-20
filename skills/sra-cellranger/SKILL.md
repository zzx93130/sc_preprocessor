---
name: sra-cellranger
description: Download single-cell raw FASTQ data from NCBI SRA/SRR accessions, stage Cell Ranger-compatible FASTQ names, run 10x Genomics Cell Ranger count/multi, retain result outputs, remove large cache/intermediate files, archive raw FASTQs as zip files to local/cloud storage, and generate an auditable summary report. Use when asked to process SRA/GEO single-cell RNA-seq raw reads through Cell Ranger, manage FASTQ archives, clean run artifacts, or summarize Cell Ranger results.
---

# SRA Cell Ranger

This Codex skill now includes the practical Docker Cell Ranger conventions from
the Codex `sra-to-10x-cellranger` skill: linux/amd64 Cell Ranger execution,
Docker Desktop/Rosetta preflight checks, explicit mounting of staged FASTQ
symlinks and their real source directories, compact `outs/` preservation, and
simple chemistry/read-length checks.

## Existing Windows GEO deployments

For a new Windows installation, use the repository's `docs/WINDOWS-DEPLOYMENT.md`
and `install-windows.ps1` (repository: https://github.com/zzx93130/sc_preprocessor).
Installing this skill alone does not install the production queue or dashboard.
Do not use the standalone helper as its scheduler. For continuing, recovering,
monitoring, or auditing that Windows Docker workflow, read
[references/windows-geo-workflow.md](references/windows-geo-workflow.md) first.
Use the deployed production stage machine and its checkpoints rather than
starting the standalone helper as a second scheduler. Machine paths, resource
limits, current holds and temporary recovery workers are deployment-specific;
revalidate runtime state rather than copying historical PIDs or sample status.
For that mode, the Windows reference takes precedence over historical standalone
examples below, especially output retention, stage ownership and local archives.

For this deployment the user chose **one whole preprocessing sample at a time,
with an independent concurrent download**. The compute slot covers conversion,
Cell Ranger, loom, archiving, verification and cleanup, not individual tools.
Honor requested monitoring intervals: a four-hour cadence is not permission
for minute-by-minute polling, repeated process waits, or status messages.
Automatic continuation prompts do not move the next check earlier.

## Quick Start

Use `scripts/sra_cellranger_pipeline.py` for repeatable SRA-to-Cell-Ranger runs. Start with `--dry-run` and a small accession list before launching a large job.

```bash
python ./scripts/sra_cellranger_pipeline.py \
  --sample-sheet samples.csv \
  --reference /path/to/refdata-gex-GRCh38-2024-A \
  --outdir /path/to/project/cellranger_run \
  --storage s3://bucket/project/raw_fastq_zip \
  --localcores 16 \
  --localmem 128 \
  --dry-run
```

`samples.csv` must include `accession` and `sample` columns. Optional columns: `read_map`, `expected_cells`, `chemistry`, `extra_args`.

## Workflow

1. Confirm inputs before running:
   - SRA/SRR accessions are raw single-cell 10x-style reads, not processed matrices.
   - Cell Ranger reference matches organism and assay.
   - The user has enough disk for SRA cache, decompressed FASTQs, Cell Ranger intermediates, results, and FASTQ zip archives.
   - Storage destination is writable. Supported helper targets: local directory, `s3://` via AWS CLI, `gs://` via gsutil, or any `rclone:` remote.

2. Download raw reads:
   - Use SRA Toolkit `prefetch` and `fasterq-dump --split-files`.
   - Keep download logs per accession.
   - Prefer accession-to-sample mapping from a sample sheet; do not infer biological sample grouping from accession names alone.

3. Stage FASTQs for Cell Ranger:
   - Cell Ranger expects names like `SAMPLE_S1_L001_R1_001.fastq.gz`.
   - Use `read_map` when SRA read numbering differs from Cell Ranger read roles, for example `1=R1,2=R2` or `1=I1,2=R1,3=R2`.
   - When uncertain about read roles, inspect read lengths and SRA/GEO metadata before running Cell Ranger.
   - If staged files are symlinks, Docker must also mount the real source directory at the same host path so the links resolve inside the container.

4. Run Cell Ranger:
   - Default to `cellranger count` for gene expression libraries.
   - Use `cellranger multi` only when the user provides a valid multi config CSV; see `references/cellranger_inputs.md`.
   - Pass `--localcores`, `--localmem`, `--expect-cells`, `--chemistry`, and extra args only when appropriate for the dataset.
   - On Apple Silicon/macOS with 10x Linux Cell Ranger, use Docker `--platform linux/amd64`; verify `cellranger --version` inside the container before long runs.
   - Docker Desktop should have enough memory for the reference. Human Cell Ranger runs are much safer with at least 32 GiB allocated.
   - For matrix-only GEX, `--create-bam=false` may save space. Keep `--create-bam=true` when BAM or velocyto loom is required; preserve explicit user choices. Use intron inclusion only when appropriate and `--disable-ui` for unattended runs.

5. Retain results and clean caches:
   - Copy or preserve `outs/` for each Cell Ranger sample under `results/<sample>/outs`.
   - For disk-limited workflows, keep only `raw_feature_bc_matrix/`, `filtered_feature_bc_matrix/`, `web_summary.html`, `metrics_summary.csv`, and `molecule_info.h5`.
   - Keep logs and generated report.
   - Remove SRA cache, temporary decompressed FASTQs, and Cell Ranger pipestance intermediates after confirming outputs and FASTQ archive upload.

6. Archive raw FASTQs:
   - Create one zip per sample from staged raw FASTQ `.gz` files.
   - Upload the zip to the requested storage destination.
   - Record checksums, zip paths, upload commands, and upload status in the report.

7. Generate a summary report:
   - Include accessions, sample mapping, commands, tool versions, output paths, storage destinations, cleanup actions, failures, and Cell Ranger metrics from `metrics_summary.csv`.

## Project Operating Experience

These notes capture lessons from the IPF SRP556117/GSE286182 processing run on
Windows with Docker Desktop, Cell Ranger 10, SRA Toolkit, and velocyto/scVelo
loom generation. Prefer these conventions when the user asks to continue,
recover, monitor, or summarize that style of project.

### Directory Layout

- Keep a stable project root such as `E:\IPF_data\cellranger`.
- Use stable output directories:
  - `raw_fastq/` for gzipped `{SRR}_*.fastq.gz` extracted from SRA.
  - `fastq_input/<sample>/` for Cell Ranger-compatible staged FASTQ names.
  - `outputs/<sample>/` for the active Cell Ranger pipestance.
  - `results/<sample>/outs/` for retained downstream Cell Ranger outputs.
  - `loom/<sample>/<sample>.loom` for velocyto/scVelo output.
  - `logs/` for launcher logs and per-run `run_*.log` files.
  - `reports/` for inventory snapshots and audit CSVs.
- Treat `results/<sample>/outs` and `loom/<sample>/<sample>.loom` as the
  durable deliverables. `outputs/<sample>` is a working pipestance unless the
  user asks to keep it for debugging or resume.

### Windows and Docker Execution

- On Windows, launch long jobs through a small PowerShell launcher script and a
  Windows scheduled task. Immediately disable the scheduled task trigger after
  it starts so a future trigger cannot duplicate the run while the process is
  still active.
- Include Docker preflight information in launcher logs:
  - `docker.exe info` memory/CPU availability.
  - `docker.exe system df` before and after large batches.
  - newest `run_*.log` path and scheduled task name.
- Docker Desktop storage may fill `C:`. If that happens, move the Docker Desktop
  WSL disk image through Docker Desktop settings to another disk, for example
  `D:\docker\Disk_image\DockerDesktopWSL`, then restart Docker/WSL and verify
  `docker.exe info`.
- Human Cell Ranger with BAM and loom is much more reliable with at least
  32 GiB Docker memory. In this project, 12 cores and 38 GiB memory were a good
  Windows/Docker default after Docker storage was moved off `C:`.

### Batch Strategy

- Process bounded batches, typically 1 to 3 samples, instead of launching the
  entire study. This keeps disk pressure manageable and makes failed samples
  recoverable without rerunning completed samples.
- Before launching, write an inventory CSV with sample, accessions, missing
  local SRA files, expected cells, and whether local inputs are complete.
- If the user says newly downloaded data are in the project root, check for the
  matching SRR/SRA directory or file before launching. Do not assume every
  listed GSM has its SRR present locally.
- If `fastq_input/<sample>` already exists and contains at least two `.fastq.gz`
  files, a recovery run can use staged FASTQs directly with an explicit
  "use existing FASTQ input" mode.

### SRA and FASTQ Validation

- `vdb-validate` is useful for suspected incomplete/corrupt SRA downloads, but
  it is not mandatory before every run if `fasterq-dump` succeeds and the
  downstream FASTQs pass sanity checks. Resumed/suspect downloads in the
  Windows HTTPS fallback must pass its full validation before publication.
- For SRA files:
  - Use `vdb-validate <SRR-or-path>` when a download was interrupted, disk space
    ran out, fasterq-dump returns exit code 3, or file size looks suspicious.
  - If `vdb-validate` fails, redownload or restore that accession before running
    Cell Ranger.
- For FASTQ `.gz` files:
  - Run `gzip -t` or equivalent to verify gzip integrity.
  - Inspect first read lengths from each split FASTQ. For 10x 3' v3 in this
    project, barcode/UMI reads are typically 26 to 30 bp and cDNA reads are
    longer than 32 bp. Technical index reads may be 8 to 10 bp and should not be
    staged as R1/R2 for Cell Ranger count.
  - Confirm paired staged names match Cell Ranger expectations:
    `SAMPLE_S1_L001_R1_001.fastq.gz` and `SAMPLE_S1_L001_R2_001.fastq.gz`.
  - If only two FASTQs exist for a sample, that can be correct for 10x data when
    the SRA split produced a barcode/UMI read and cDNA read only. Base the
    decision on read lengths and metadata, not on file count alone.

### Cell Ranger Defaults That Worked

- Use `cellranger count` for GEX libraries with:
  - `--chemistry=SC3Pv3` when metadata and read lengths match 10x 3' v3.
  - `--include-introns=true` for single-nucleus or when intronic counts are
    desired.
  - `--create-bam=true` when velocyto/scVelo loom generation is required.
  - `--disable-ui`.
  - `--localcores` and `--localmem` matched to Docker Desktop allocation.
- Preserve compact downstream outputs in `results/<sample>/outs`, including:
  `filtered_feature_bc_matrix/`, `raw_feature_bc_matrix/`, H5 matrices,
  `web_summary.html`, `metrics_summary.csv`, `molecule_info.h5`, optional
  `cloupe.cloupe`, and BAM/BAI when loom generation or downstream BAM use is
  needed.

### Velocyto Loom Generation

- Generate loom only after Cell Ranger completed with a valid
  `possorted_genome_bam.bam`; run `samtools quickcheck` before using the BAM.
- Use the Cell Ranger reference GTF from `refdata-gex-GRCh38-2024-A/genes`.
  If only `genes.gtf.gz` exists, decompress it into a temporary Linux-visible
  path for velocyto.
- For Cell Ranger 10 BAMs, velocyto can fail or overuse memory unless the BAM
  is filtered to reads containing both `CB:Z:` and `UB:Z:` tags. A robust loom
  workflow:
  - `samtools quickcheck` the original BAM.
  - Create a CB/UB-filtered BAM with `samtools view -h | awk ... | samtools view -b`.
  - `samtools quickcheck` the filtered BAM.
  - Reuse an existing filtered or cell-sorted BAM only if `quickcheck` passes.
  - Skip re-sorting if `cellsorted_possorted_genome_bam.bam` already exists and
    passes `quickcheck`.
- On Windows Docker, run velocyto in a Docker volume or Linux-backed workspace
  and copy the final loom back to `loom/<sample>/<sample>.loom`. Avoid assuming
  velocyto writes directly to the host path unless the script explicitly copies
  `/sample/velocyto/*.loom` to the mounted output directory.
- Typical velocyto settings:
  - Normal: `-@ 4 --samtools-memory 1500`.
  - Recovery after memory kill/exit 137: use 1 thread, lower samtools memory
    such as 500 to 750 MB, and increase Docker/WSL memory before retrying.
- Treat exit code 137 or a `Killed` line as a memory kill. Treat exit code 2
  after "completed without a loom file" as an output-copy or path issue if the
  velocyto log says "Terminated Succesfully!".

### Monitoring and Completion Checks

- To check status, inspect in this order:
  - Presence and size of `results/<sample>/outs/metrics_summary.csv`.
  - Presence and size of `loom/<sample>/<sample>.loom` when loom was requested.
  - Newest `logs/run_*.log` tail.
  - Active Docker containers and scheduled task/process state.
- A sample with Cell Ranger metrics but no loom is not complete if loom was
  requested. Do not tell the user to move `fastq_input/<sample>` until the loom
  exists or the user decides to skip loom generation.
- When reporting a completed sample, include:
  - Final `results/<sample>/outs` path.
  - Key metrics from `metrics_summary.csv`: estimated cells, mean reads/cell,
    median genes/cell, median UMI/cell, total reads, valid barcodes, Q30 RNA
    read, and fraction reads in cells.
  - Loom path if available.
  - Whether `fastq_input/<sample>` exists and can be moved to other storage.
- If a run is still active, report the active sample and newest meaningful log
  progress, for example Cell Ranger stage, velocyto BAM sorting, intron scan,
  first N million reads, or counting batch number. Do not duplicate or restart
  healthy work. Respect the deployment's independent download/preprocessing
  limits; a healthy compute job does not prohibit its authorized download lane.

### Recovery Playbook

- Disk pressure:
  - Free space before retrying.
  - Check Docker storage with `docker.exe system df`.
  - Move Docker Desktop WSL data to a larger disk if `C:` fills.
  - Restart Docker Desktop/WSL after storage or memory changes and verify
    `docker.exe info`.
- Interrupted Cell Ranger:
  - Prefer a resume script if the pipestance still exists and Cell Ranger can
    resume cleanly.
  - If only compact results remain, rerun from `fastq_input/<sample>` when
    staged FASTQs are still available.
- Failed fasterq-dump:
  - Check disk space and temporary directory.
  - Validate or redownload the SRA.
  - Reuse completed `.fastq.gz` files larger than a reasonable threshold instead
    of re-extracting.
- Failed velocyto:
  - Check whether Cell Ranger RNA outputs are already valid.
  - Check original and intermediate BAMs with `samtools quickcheck`.
  - Retry loom-only if Cell Ranger outputs are good.
  - Lower velocyto threads/memory for exit 137, and reuse quickcheck-passing
    filtered/cell-sorted BAMs to avoid repeated expensive sorting.
- Docker hang or EOF:
  - Confirm whether the scheduled task, PowerShell launcher, and Docker
    container are still active.
  - If stopped before completion, restart Docker Desktop and rerun only the
    failed sample or loom-only stage.

### Server Deployment

- For samples that repeatedly fail on Windows due to Docker memory or disk,
  move processing to a Linux server:
  - Transfer the local SRA files or staged `fastq_input/<sample>` directory.
  - Transfer or install the same Cell Ranger reference.
  - Install Cell Ranger, SRA Toolkit, samtools, velocyto, and Python/loompy
    dependencies.
  - Run a Linux-native script that stages FASTQs, runs `cellranger count` with
    BAM enabled, creates a CB/UB-filtered BAM for velocyto, writes the loom, and
    copies compact outputs back.
  - Bring back `results/<sample>/outs`, `loom/<sample>/<sample>.loom`, logs, and
    a metrics summary.

## Bundled Resources

- `scripts/sra_cellranger_pipeline.py`: orchestrates download, staging, Cell Ranger execution, archive/upload, cleanup, and report generation. Use `--dry-run` to print actions without executing external tools.
- `scripts/stage_10x_fastqs.py`: stage existing `{SRR}_1.fastq.gz`/`{SRR}_2.fastq.gz` files into Cell Ranger naming. Supports grouping multiple runs under one sample.
- `scripts/run_cellranger_count_docker.sh`: Docker linux/amd64 `cellranger count` wrapper with symlink source mounts, version/memory preflight, optional chemistry autodetection, and compact output copy.
- `scripts/copy_cellranger_outputs.py`: copy the compact downstream Cell Ranger artifacts from run directories into a stable results directory.
- `references/cellranger_inputs.md`: read when configuring sample sheets, read maps, `cellranger count`, `cellranger multi`, or storage destinations.

## Operating Rules

- Do not delete raw FASTQs or SRA cache until zip creation and upload have succeeded, unless the user explicitly accepts that risk.
- Do not recommend moving `fastq_input/<sample>` until all requested deliverables
  for that sample exist, especially loom files when `--create-bam`/velocyto was
  requested.
- Treat SRA/GEO metadata as authoritative for sample grouping, chemistry, organism, and read layout.
- For large studies, process one sample at a time or in bounded batches so failed samples do not force rerunning successful ones.
- Prefer targeted recovery: retry only the failed sample or loom-only stage once
  Cell Ranger metrics and BAM are already valid.
- For Windows scheduled-task launches, disable future triggers after a task
  starts and monitor logs instead of starting duplicate jobs.
- If network, SRA Toolkit, Cell Ranger, or storage credentials are unavailable, generate the exact commands and a report template instead of pretending the run completed.
