---
name: sra-cellranger
description: Download single-cell raw FASTQ data from NCBI SRA/SRR accessions, stage Cell Ranger-compatible FASTQ names, run 10x Genomics Cell Ranger count/multi for scRNA-seq and snRNA-seq, Cell Ranger ATAC for snATAC-seq, or Cell Ranger ARC for Multiome (joint snATAC + snRNA), retain result outputs, remove large cache/intermediate files, archive raw FASTQs as zip files to local/cloud storage, and generate an auditable summary report. Use when asked to process SRA/GEO single-cell or single-nucleus RNA-seq, snATAC-seq/scATAC-seq, or 10x Multiome raw reads through Cell Ranger, Cell Ranger ATAC or Cell Ranger ARC, manage FASTQ archives, clean run artifacts, or summarize results.
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

Use the Windows reference for direct HTTPS configuration, checkpoint recovery,
copy-only and release-after-verification NAS plans, and final reconciliation
against the input CSV. Report processing, structural audit and archive
verification counts separately. Apply the local retention policy for each plan.

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

`samples.csv` must include `accession` and `sample` columns. Optional columns: `read_map`, `expected_cells`, `chemistry`, `extra_args`, `assay`, `library_type`.

## snATAC-seq and Multiome (snATAC + snRNA)

The standalone helper handles three assays, chosen per sample with the `assay`
column (or `--assay` with `--accessions`). Read
[references/atac_multiome_inputs.md](references/atac_multiome_inputs.md) before
running either new assay.

| Data | `assay` | Tool | Reference flag |
| --- | --- | --- | --- |
| scRNA-seq / snRNA-seq | `rna` (default) | `cellranger count` | `--reference` (`refdata-gex-*`) |
| snATAC-seq | `atac` | `cellranger-atac count` | `--atac-reference` (`refdata-cellranger-arc-*`) |
| Multiome, both modalities from the same nuclei | `multiome` | `cellranger-arc count` | `--atac-reference` |

```csv
accession,sample,assay,library_type,read_map
SRR000010,donor1,multiome,Gene Expression,
SRR000011,donor1,multiome,Chromatin Accessibility,auto
SRR000020,lung_atac_1,atac,,
SRR000030,nuclei_rna_1,rna,,
```

```bash
python ./scripts/sra_cellranger_pipeline.py \
  --sample-sheet samples.csv \
  --reference /path/to/refdata-gex-GRCh38-2024-A \
  --atac-reference /path/to/refdata-cellranger-arc-GRCh38-2020-A-2.0.0 \
  --outdir /path/to/project/run \
  --storage /path/to/raw_fastq_zip \
  --localcores 16 --localmem 64 \
  --dry-run
```

Rules that differ from the RNA workflow:

1. Classify each GSM first. Multiome means one ATAC and one GEX library from
   the same GEM well; pair them under one `sample` with `library_type` on every
   row. snRNA and snATAC from separate nuclei preps are two samples (`rna` and
   `atac`). `cellranger-arc` cannot run a single modality.
2. ATAC needs three reads: R1 and R3 genomic, R2 the 16 bp cell barcode (24 bp
   for Multiome ATAC), plus an optional 8 bp I1. SRA flags the barcode read as
   technical, so `fasterq-dump` needs `--include-technical`; the helper adds it
   for `atac` and `multiome`. Check lengths with
   `scripts/inspect_fastq_reads.py --assay atac` or set `read_map` to `auto`.
   The default ATAC map is `1=R1;2=R2;3=R3`; a four-file dump is usually
   `1=I1;2=R1;3=R2;4=R3`.
3. If only two long reads exist, the barcode was not deposited as FASTQ. Stop
   and look for the submitter's original FASTQ/BAM files (SRA "Data access"
   tab, ENA, `bamtofastq`) instead of running Cell Ranger.
4. Multiome FASTQs are staged as `<sample>/gex/<sample>_gex_*` and
   `<sample>/atac/<sample>_atac_*` with a generated `libraries.csv`.
5. BAM: `cellranger-arc` gets `--create-bam=false` unless `--create-bam true`
   is passed (`--arc-legacy-bam-flag` for ARC 2.0, which uses `--no-bam`).
   `cellranger-atac` always writes a BAM. `expected_cells` is ignored for both.
6. Retain the fragments file **and its `.tbi`**
   (`fragments.tsv.gz` / `atac_fragments.tsv.gz`), peak or feature matrices,
   `summary.csv` and `web_summary.html`. Completion is judged on `summary.csv`
   and a non-empty fragments file, not `metrics_summary.csv`.
7. Both tools need about 64 GB RAM for human references; set `--localmem` and
   Docker Desktop memory to match.
8. For snRNA-only samples keep intron counting on (default from Cell Ranger 7;
   `--include-introns=true` otherwise).
9. The production queue in `pipeline/scpre` routes each sample by its
   technology label (`rna`, `snrna`, `atac`, `multiome`); see "Technology label
   in the production queue" below. The PBS cluster mode is still `rna`/`snrna`
   only (the label is passed to the worker) and skips `atac`/`multiome`
   samples with a warning.

### Technology label in the production queue

Every queue sample has `meta.tech`, shown as a badge on the dashboard. Samples
registered before the label existed count as `rna` and behave as before.

| Label | Count tool | Loom | Needs in `config.yaml` `paths` |
| --- | --- | --- | --- |
| `rna` (default) | `cellranger count` | yes | `cellranger_dir`, `reference_dir` |
| `snrna` | `cellranger count --include-introns=true` | yes | same |
| `atac` | `cellranger-atac count` | no | `cellranger_atac_dir`, `atac_reference_dir` |
| `multiome` | `cellranger-arc count` | no | `cellranger_arc_dir`, `atac_reference_dir` |

Where the label comes from, highest priority first:

1. `metadata.tech` in `config.yaml`, keyed by sample id or run:
   `tech: {GSM123: atac, GSM124: snrna}`.
2. A `tech` column added to `SraRunTable.csv`.
3. `Assay Type` / `LibraryStrategy` = `ATAC-seq` gives `atac` automatically; a
   GSM holding both ATAC and RNA runs gives `multiome`.
4. `pipeline.default_tech` (default `rna`). `snrna` is never inferred; label it.

Multiome with separate ATAC and GEX GSMs needs pairing into one sample:
`metadata.multiome_pairs: {donor1: [GSM200, GSM201]}`, or a shared value in a
`multiome_sample` column. The pair is registered under the new id and the
member GSMs are not queued on their own. Library type per run comes from
`Assay Type`, or `metadata.library_types: {SRR999: atac}`.

Operating rules for labels:

- Set labels **before** a sample starts. A changed label or
  `metadata.library_types` entry is applied on the next inbox scan in two
  cases only: the sample is still queued with no work directory, or it has
  failed before its count step completed. In the failed case its staged
  FASTQs are dropped and Retry reconverts them under the new label. Any later
  change is ignored with a log warning. If a pair's member GSM is already in
  the state database as its own sample, the pair is not registered; resolve
  that with the user rather than editing the database.
- A wrong label fails early in `converting` with a read-role error (for
  example an `atac` label on GEX reads). Fix the label or `metadata.read_maps`
  (`"1=R1,2=R2,3=R3"`), rescan, then Retry; do not force roles to make it pass.
- ATAC and Multiome FASTQs stay in the sample's one FASTQ zip. Multiome files
  are named `<sample>_gex_*` and `<sample>_atac_*`.
- The ATAC BAM is not archived unless `pipeline.archive_atac_bam: true`;
  Multiome BAMs are not created unless `pipeline.multiome_create_bam: true`.
  Verification checks the fragments file, its `.tbi`, the `.h5` matrix and
  `summary.csv`, and checksums the fragments file against the working copy.
- All three tools share the one Cell Ranger slot (`max_cellranger_jobs`) and
  the same cores/memory settings; ATAC and ARC want about 64 GB.
- `scpre add-sample --tech atac` registers ATAC FASTQs (role in the file name
  required); Multiome is not supported in FASTQ mode.
- The ATAC/ARC path has only been exercised with mock tools. Run one small
  real sample of each new label and audit its outputs before a full study.

## Workflow

1. Confirm inputs before running:
   - SRA/SRR accessions are raw single-cell 10x-style reads, not processed matrices.
   - Cell Ranger reference matches organism and assay.
   - The user has enough disk for SRA cache, decompressed FASTQs, Cell Ranger intermediates, results, and FASTQ zip archives.
   - Storage destination is writable. Supported helper targets: local directory, `s3://` via AWS CLI, `gs://` via gsutil, or any `rclone:` remote.

2. Download raw reads:
   - Use SRA Toolkit `prefetch` and `fasterq-dump --split-files` (plus `--include-technical` for ATAC/Multiome, or when a barcode/index read is missing).
   - Keep download logs per accession.
   - Prefer accession-to-sample mapping from a sample sheet; do not infer biological sample grouping from accession names alone.

3. Stage FASTQs for Cell Ranger:
   - Cell Ranger expects names like `SAMPLE_S1_L001_R1_001.fastq.gz`.
   - Use `read_map` when SRA read numbering differs from Cell Ranger read roles, for example `1=R1,2=R2` or `1=I1,2=R1,3=R2`.
   - When uncertain about read roles, inspect read lengths and SRA/GEO metadata before running Cell Ranger.
   - If staged files are symlinks, Docker must also mount the real source directory at the same host path so the links resolve inside the container.

4. Run Cell Ranger:
   - Default to `cellranger count` for gene expression libraries, `cellranger-atac count` for snATAC-seq, and `cellranger-arc count` for Multiome.
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
   - Archive each sample's original FASTQs, including unmatched reads when paired-only derivatives were staged. Preserve the pairing decision and original hashes.
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
- Before reusing staged FASTQs, match the complete set of expected runs,
  lanes and read roles to the staging manifest and verify the files.

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
  - Recovery after confirmed memory exhaustion: use 1 thread, lower samtools memory
    such as 500 to 750 MB, and increase Docker/WSL memory before retrying.
- For exit code 137 or a `Killed` line, check the container OOM flag and host or engine events before attributing the termination to memory pressure. Treat exit code 2
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
  - Match retained FASTQs to the expected runs and mates. Check their completion
    manifest and test uncertain gzip streams to EOF before reusing them.
- Failed velocyto:
  - Check whether Cell Ranger RNA outputs are already valid.
  - Check original and intermediate BAMs with `samtools quickcheck`.
  - Retry loom-only if Cell Ranger outputs are good.
  - Lower velocyto threads/memory for confirmed memory exhaustion, and reuse quickcheck-passing
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

- `scripts/sra_cellranger_pipeline.py`: orchestrates download, staging, Cell Ranger / Cell Ranger ATAC / Cell Ranger ARC execution, archive/upload, cleanup, and report generation. Use `--dry-run` to print actions without executing external tools.
- `scripts/stage_10x_fastqs.py`: stage existing `{SRR}_1.fastq.gz`/`{SRR}_2.fastq.gz` files into Cell Ranger naming. Supports grouping multiple runs under one sample, `--read-map` for ATAC layouts, and `--sample-suffix` for Multiome libraries.
- `scripts/inspect_fastq_reads.py`: print read lengths of split FASTQs per accession and suggest a `read_map` for `rna` or `atac`.
- `scripts/run_cellranger_atac_arc_docker.sh`: Docker linux/amd64 wrapper for `cellranger-atac count` (`atac <sample>`) and `cellranger-arc count` (`multiome <sample>`), with barcode-read length check, generated `libraries.csv`, and compact output copy.
- `scripts/run_cellranger_count_docker.sh`: Docker linux/amd64 `cellranger count` wrapper with symlink source mounts, version/memory preflight, optional chemistry autodetection, and compact output copy.
- `scripts/copy_cellranger_outputs.py`: copy the compact downstream Cell Ranger, ATAC or ARC artifacts from run directories into a stable results directory (assay auto-detected).
- `references/cellranger_inputs.md`: read when configuring sample sheets, read maps, `cellranger count`, `cellranger multi`, or storage destinations.
- `references/atac_multiome_inputs.md`: read before any snATAC-seq or Multiome run: pipeline choice, read layout, `libraries.csv`, outputs to keep, failure patterns.

## Operating Rules

- Do not delete raw FASTQs or SRA cache until zip creation and upload have succeeded, unless the user explicitly accepts that risk.
- Do not recommend moving `fastq_input/<sample>` until all requested deliverables
  for that sample exist, especially loom files when `--create-bam`/velocyto was
  requested.
- Treat SRA/GEO metadata as authoritative for sample grouping, chemistry, organism, and read layout.
- Never pair an ATAC and a GEX accession as Multiome on name similarity alone; confirm from GEO that they share a GEM well.
- Do not drop an ATAC fragments file's `.tbi`. In the production queue, confirm each sample's tech label before it starts; the PBS cluster mode accepts `rna`/`snrna` only.
- For large studies, process one sample at a time or in bounded batches so failed samples do not force rerunning successful ones.
- Prefer targeted recovery: retry only the failed sample or loom-only stage once
  Cell Ranger metrics and BAM are already valid.
- For Windows scheduled-task launches, disable future triggers after a task
  starts and monitor logs instead of starting duplicate jobs.
- If network, SRA Toolkit, Cell Ranger, or storage credentials are unavailable, generate the exact commands and a report template instead of pretending the run completed.
