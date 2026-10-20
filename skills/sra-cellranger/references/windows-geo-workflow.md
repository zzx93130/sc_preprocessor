# Windows GEO production workflow

Use for Windows single-cell installations. Paths beginning `E:\sc_preprocessor`
below are examples from the original deployment; substitute the configured
data/repository roots. See repository `docs/WINDOWS-DEPLOYMENT.md` for installation.
This is an operating reference, not an installer
or permission to start jobs, delete inputs, or change other projects. Never
reuse historical PIDs/sample status as live evidence. Standalone skill helpers
do not automatically implement the production scheduler, HTTPS fallback or
paired-only derivative logic described here.

## Authoritative state and layout

- Production: `E:\sc_preprocessor\repo\pipeline\scpre`; configuration:
  `repo/config.yaml`; Python: `repo/pipeline/.venv/Scripts/python.exe`.
- Inputs/batch definition: `to_process/SraRunTable.csv`. Derive the entire
  sample/run scope from it, not just visible files or completed samples.
- API: `http://127.0.0.1:9123/api/state`; logs: `/api/logs/<sample>?tail=N`.
  Filter large API responses to relevant fields.
- SQLite/logs: `%LOCALAPPDATA%\scpre\state.db` and `logs` (NTFS).
  Large work: `E:\sc_preprocessor\work`; durable results:
  `E:\sc_preprocessor\archive\<project>\<sample>`. Operator reports:
  `E:\sc_preprocessor\reports`; pipeline checks: configured `runs/reports`.
- Read `reports/monitoring-interval.json`, latest dated monitor and relevant
  recovery/activation reports before acting. They are historical records;
  a lock/status file alone cannot establish a live owner.

## Independent download / whole-preprocessing slots

User-selected settings for this host:

```yaml
concurrency:
  separate_downloads: true
  max_download_jobs: 1
  max_preprocessing_samples: 1
  download_ahead: 1
  max_cellranger_jobs: 1
  cellranger_cores: 10
  cellranger_mem_gb: 36
  convert_threads: 4
```

One download may overlap one **whole** post-download lifecycle. The compute
slot lasts through conversion, Cell Ranger, loom, archiving, verification and
cleanup. Do not start sample B's conversion during sample A's loom. Prefer
ready samples for the next compute slot; bound downloaded-ahead inputs to
avoid filling E:. Atomically reserve space across concurrent work/archive
jobs, including undownloaded runs and uncompressed extraction peaks.
`max_active_samples` is a legacy limit, used only when separate lanes are off.

Distinguish saved policy from loaded runtime. Read live API `config.concurrency`
and any deployment activation records; do not assume activation succeeded.
Editing YAML/source is not live reload. Deploy only at a verified idle service
boundary, without stopping healthy tools or restarting Docker to force it.

`paused from dashboard` is a sticky operator hold; environment pauses differ.
Existing downloads may continue until stage boundaries. A hold can protect an
external priority worker: do not resume blindly. API `active_workers` excludes
external one-shot workers. Reconcile sample state, actual containers, native
curl/Python, scheduled tasks and ownership reports. A venv launcher plus its
runtime child is not necessarily duplicate schedulers. Service startup recovery
may stop orphan containers: do not restart while an external worker owns one.

Temporary priority/download workers are scoped exceptions, not another queue.
An authorized recovery requires real terminal failure, no live owner, duplicate
protection, space reservations and production stage/checkpoint reuse. Download-
only recovery yields before conversion and preserves the compute hold. Do not
keep adding one-off workers instead of enabling the independent scheduler.

## Secure resumable SRA downloads

Use production SRA Toolkit first. Its known Windows certificate failure can
fall back to verified HTTPS:
`https://sra-pub-run-odp.s3.amazonaws.com/sra/<SRR>/<SRR>` with the configured
proxy. Never bypass TLS validation; curl certificate error60 is not an ordinary
transport retry. Do not change proxy trust or globally disable certificates.

Preserve `<SRR>.sra.part`. Production `--continue-at -` recalculates offsets
from retained bytes on each bounded transport retry (currently up to3 retries).
Error56/missing close_notify indicates transport closure, not disk corruption.
After exhausted retries, inspect terminal state/logs, actual owner absence,
disk space/events and, if useful, a small HTTP206 range at the exact offset
before one targeted resume. Do not retry indefinitely without investigation.
An observation timeout is not a stopped job; changing curl PIDs may be normal.

Before publication require expected byte size, `NCBI.sra` magic and successful
`vdb-validate`, then atomic rename and download sentinel. A browser `.man`
suffix alone does not identify the payload; inspect and validate contents.
Renaming by itself neither repairs nor proves a valid SRA.

## FASTQ checkpoints, read roles and missing mates

Production stages: downloading → converting → cellranger → loom → archiving →
verifying → cleaning → done. Reuse valid stage and atomic fasterq completion
manifests. After interrupted compression, retain completed raw FASTQs and
validated mate gzip; replace only a proven incomplete derivative when safe.
`gzip_ok`, magic, existence and size are cheap checks, **not full gzip CRC**.
Validate uncertain retained gzip to EOF, with expected decoded size/read
records where known, before relying on it. Retain originals through archive
verification; never delete a source merely because a derivative file exists.

Do not infer read roles from split numbers or lengths alone. This study has
equal151+151 reads. Use metadata/chemistry, poly-T and actual barcode-whitelist
match rates. Record whitelist identity/hash, inspected counts, match counts,
chosen map and rationale with logs/repro. Persist approved per-run read_maps;
investigate ambiguous evidence instead of forcing chemistry from another study.

Unequal mate counts can be genuine SRA singletons. Diagnose valid source data
and actual FASTQ spot IDs, not just counts/order. Never truncate, fabricate
mates, silently discard reads or claim singletons have no biological value.
With user approval, production `paired_input.py` creates separate paired-only
analysis inputs by matching IDs, checks expected pair/singleton counts and
original SHA256 before/after, and commits atomic derivatives/decision manifest.
Archive **all original FASTQs**, including unmatched R1, plus decision and
reproduction helper. The approved SRR9985349 exception is not a default for
other samples; do not extrapolate it without new evidence/authorization.

## Windows storage and safe recovery

E: here is exFAT. SQLite/logs stay on NTFS; Martian's symlink-heavy pipestance
uses Linux/ext4 Docker named volumes rather than an E: working bind mount.
The VHD under `E:\sc_preprocessor\docker-storage` is live Docker storage,
not a disposable backup. Verify real Docker settings/mounts after disk swaps.
Open-file sizes/timestamps can lag; process write counters or Linux fdinfo
offsets can establish progress. Query bounded recent storage events/free space;
absence of errors is not a future hardware-stability guarantee.

Do not enable macOS virtiofs cleanup restarts on this Windows host:
`restart_docker_desktop_at_idle: false`. A work directory is not removable
solely because one stage finished. Preserve failed/resumable/pending data and
user-retained backups; remove only explicitly verified disposable targets.

## Completion gates and audits

Cell Ranger metrics alone are not completion when loom was requested. Keep
BAM enabled for this workflow. Reuse only quickcheck-passing BAM/sorted BAM;
loom uses Linux-backed work and the implemented CB/UB filtering. Verify the
final copied loom in the durable archive. OOM/137 warrants targeted loom
recovery with reduced memory pressure, not an automatic whole-sample rerun.

Before accepting completion, cover **all expected runs** and verify:

- Raw/filtered MTX, features, barcodes and H5 open with consistent dimensions;
  metrics, web summary and molecule_info H5 are retained/readable.
- BAM/BAI retained; BAM passes samtools quickcheck, index present and usable.
  Presence alone does not prove validity; make checks match the claim.
- Loom opens with spliced/unspliced/ambiguous layers; cell count matches the
  filtered matrix, and compare cell identities when available.
- Original FASTQs all represented in ZIP/manifest with exact names, sizes and
  SHA256 records; recomputed ZIP SHA256 and ZIP CRC/integrity pass. ZIP CRC
  alone does not prove integrity of inner gzip streams.
- Logs, tools/reference identity, read-role/pairing decisions, checks and
  reproduction materials retained. Paired-only original hashes must match
  the raw-archive manifest, not merely the analysis derivatives.

Production `verifying` covers ZIP SHA/CRC/names, selected archive existence/
copy hashes and paired-original preservation. It does **not by itself prove**
every HDF5/BAM/index/loom structural gate. Inspect actual verifier coverage;
use repository `deploy/validate_outputs.py` for structural checks, plus an
independent full ZIP checksum/CRC/manifest audit for relevant completed samples.
The original host's `reports/audit_completed_geo.py` is not a portable dependency.
The validator container needs its explicit Python entrypoint; see the deployment
guide. Do not repeatedly hash huge unchanged archives without a
changed-evidence or final batch-audit need.

Only after required output/archive gates may configured cleanup remove temp
data. Local archive is valid; upload verification applies when remote storage
was requested. Preserve inbox SRA when remove_inbox=false. Final report must
reconcile the entire CSV with per-sample metrics/paths/checks, exceptions and
cleanup outcomes, not just done samples. Stop the existing batch monitor only
after the full requested scope passes.

## Four-hour monitoring means no continuous polling

Use the existing recurring monitor, not duplicate automations. Read last/next
check times. **If not due, do not poll jobs, loop on50-second process waits or
send repetitive progress messages because an automatic continuation arrived.**
Explicit user requests or actionable failure evidence can justify earlier
checks; generic continuation cannot. Do not mark healthy waiting goals paused,
blocked or complete to suppress continuation.

At a due check take a bounded queue/owner, container/process, meaningful-log,
disk-space/event snapshot. Correlate long stages with real processes/counters.
Timeout observing a handle is inconclusive: safely re-observe the same handle
or another authoritative source, never restart solely for an expired query.
On proven failure recover only within current authority, preserve data and
verify recovery once. Record timestamp/next check, scope/counts, stage and
evidence. Quiet unchanged/healthy results unless periodic reports were
explicitly requested; notify meaningful changes, completion, recovery, failure
or required user action. Generic automation text does not authorize frequent
messages. Monitoring itself grants no new mutation or deletion permissions.
