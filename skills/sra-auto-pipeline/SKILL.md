---
name: sra-auto-pipeline
description: Operate a resumable single-cell SRA/FASTQ pipeline that discovers SraRunTable.csv mappings, stages 10x FASTQs, runs Cell Ranger, generates GEX loom files with velocyto, archives raw reads, validates durable outputs, cleans intermediates, and monitors or retries jobs. Use when processing SRA/GEO single-cell data, diagnosing pipeline/dashboard/Docker failures, or managing its archives and results.
---

# SRA Auto Pipeline

Use this skill from the pipeline project root. It is a standard `SKILL.md`
workflow and is intentionally vendor-neutral so both Codex and Claude can use
it.

## Preflight

1. Inspect `config.json`, `package.json`, and the configured `inboxDir` and
   `outRoot`; do not assume that a similarly named inbox elsewhere is active.
2. Confirm Node.js 18.17 or newer, Docker Desktop/Engine, the Cell Ranger
   executable or Linux mount, the configured GEX/ATAC reference, and enough
   free space for decompressed reads, BAM, loom sorting, and archives.
3. Run `npm run dry-run` before a new installation or after code/configuration
   changes. This uses local fixtures and must not download data or start
   Docker containers.

## Input and metadata

- Put complete `.sra` files, SRA directories, or split FASTQ files in the
  configured `inboxDir`. The watcher waits for size and modification time to
  stabilize before registering a run.
- For GEO/SRA downloads, keep `SraRunTable.csv` beside the inbox, inside the
  inbox, or at the project root. The pipeline automatically discovers it and
  prefers its Run-to-sample mapping over network metadata. Use
  `metadata.sraRunTablePath` or `SRA_AUTO_SRA_RUN_TABLE` for an explicit path.
- Verify the table's run accession, sample name, study, organism, library
  strategy/source, and any read-role information before processing a large
  sample. Use `readMaps` for authoritative overrides such as
  `"SRR123": "1=I1,2=R1,3=R2"`.

## Start and monitor

From the project root:

```powershell
npm start
```

Open the configured dashboard (usually `http://localhost:9123`). The useful
read-only endpoints are:

- `GET /api/state` — samples, queue, references, warnings, and disk usage.
- `GET /api/logs/<sample>?tail=200` — recent sample log and parsed stage.
- `GET /api/disk` — free/total space for the active output root.

Process one biological sample at a time. A normal GEX path is
`staging -> archiving -> running -> loom -> collecting -> cleaning -> done`.
Do not declare success until the dashboard reports `done` and all durable
deliverables validate.

## Durable outputs and cleanup

For each sample, verify:

- `archives/<sample>.fastq.zip`, its `.sha256`, and its manifest;
- `results/<sample>/outs` with valid 10x matrices, H5 files, metrics, and the
  requested BAM;
- `loom/<sample>/<sample>.loom` for GEX;
- the audit manifest and reports under `runs/reports`.

Working directories (`fastq_input`, `raw_fastq`, and Cell Ranger `outputs`) are
removed only after archive, matrix, loom, and pre-cleanup manifest validation.
Keep `cleanup.removeInbox=false` during an initial production trial if an extra
copy of the input is required. Never manually delete a failed sample's work
volume or intermediates before inspecting its log and state.

## Retry and recovery

- Restarting the service recovers interrupted active stages and reuses verified
  archives, staged FASTQs, Cell Ranger outputs, and valid loom files.
- After correcting a reported cause, retry only a `failed` or `blocked` sample:

  ```powershell
  Invoke-RestMethod -Method Post `
    -Uri 'http://localhost:9123/api/retry/<sample>'
  ```

- A failed loom run retains the Docker work volume so the next attempt can
  reuse filtered BAM work. Preserve that volume while diagnosing the failure.

## Troubleshooting

- **Docker daemon unavailable or permission denied:** start Docker Desktop,
  verify `docker version`, and run the pipeline service with the same Windows
  privilege that can access the Docker engine pipe. Do not move Docker's data
  disk unless the user explicitly requests a storage migration.
- **`fasterq-dump` exit 3 / disk-limit:** check the output and temporary disks,
  remove only recoverable stale files, and retry after confirming space.
- **Cell Ranger exit 1:** inspect the sample log, reference path, FASTQ naming,
  memory, and Docker mounts. Do not delete validated outputs before checking
  whether they can be reused.
- **Velocyto exit 125 or unexpected EOF:** check Docker Engine health, free
  space, the retained `sra_auto_velocyto_<sample>` volume, and the last log
  line. Retry after the engine is healthy.
- **No dashboard changes:** verify the service process, port, configured
  inbox, and `runs/service.stdout.log`; a static HTML file does not itself run
  the watcher.

When reporting status, include the sample ID, state, arrived/expected runs,
current stage, exact error, output paths, and the last meaningful log line.
Avoid exposing credentials or private tokens in logs, commits, or skill text.

