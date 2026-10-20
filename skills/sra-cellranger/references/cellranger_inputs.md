# Cell Ranger Input Notes

## Sample sheet

Use CSV with at least:

```csv
accession,sample
SRR000001,treated_1
SRR000002,treated_1
SRR000003,control_1
```

Optional columns:

- `read_map`: comma-separated mapping from SRA read number to Cell Ranger role, such as `1=R1,2=R2` or `1=I1,2=R1,3=R2`.
- `expected_cells`: value for `cellranger count --expect-cells`.
- `chemistry`: value for `cellranger count --chemistry`.
- `extra_args`: additional Cell Ranger arguments for one sample. Keep this simple; avoid shell quoting tricks in CSV.

## Read mapping

SRA read numbering is not guaranteed to match Cell Ranger roles. Confirm with GEO/SRA metadata and read lengths.

Common layouts:

- Two-read 10x 3' GEX: often `1=R1,2=R2`, where R1 contains barcode/UMI and R2 contains transcript sequence.
- Three-read layouts: often include an index read, for example `1=I1,2=R1,3=R2`, but this must be verified.

If read roles are ambiguous, inspect the first records with `seqkit stats` or `zcat FASTQ | head`, compare read lengths with the relevant 10x chemistry, and check the linked GEO supplementary methods.

Quick R1 length check:

```bash
gzip -dc SAMPLE_1.fastq.gz | sed -n '2p' | awk '{ print length($0) }'
```

Common chemistry hints:

- 26 bp R1 often indicates 10x 3' v2 (`SC3Pv2`).
- 28 bp R1 often indicates 10x 3' v3 (`SC3Pv3`).
- 5' GEX datasets may need an explicit 5' chemistry such as `SC5P-R2`; do not overwrite known GEO/library metadata with length guessing.

## Cell Ranger count

Required:

- `--id`: stable sample identifier.
- `--transcriptome`: Cell Ranger reference directory.
- `--fastqs`: staged FASTQ directory.
- `--sample`: sample prefix used in FASTQ names.

Useful optional arguments:

- `--localcores`
- `--localmem`
- `--expect-cells`
- `--chemistry`
- `--include-introns` for pre-mRNA or single-nucleus workflows when justified.

Docker linux/amd64 notes:

- Apple Silicon Macs can run 10x Linux Cell Ranger through Docker Desktop with `--platform linux/amd64`.
- Verify the binary before a long run:

```bash
docker run --rm --platform linux/amd64 \
  -v "$CR_BIN_DIR":/opt/cellranger:ro \
  debian:bookworm-slim /opt/cellranger/bin/cellranger --version
```

- Cell Ranger human-reference runs are memory hungry. Allocate at least 32 GiB to Docker Desktop when possible; a smaller `--localmem` may work for small tests but can fail during STAR.
- If staged FASTQs are symlinks, mount both the staged directory and each symlink target directory at the same host path inside Docker. Otherwise Cell Ranger may report "No input FASTQs were found" even when the staged names look correct on the host.
- `--create-bam=false` saves substantial time and disk when downstream work only needs matrices.

## Cell Ranger multi

Use `cellranger multi` when the library structure requires a multi config CSV, such as feature barcoding, multiplexing, or paired GEX/VDJ workflows. Do not synthesize a multi config unless the user provides library metadata.

## Storage destinations

The helper supports:

- Local directory path: uses filesystem copy.
- `s3://bucket/prefix`: uses `aws s3 cp`.
- `gs://bucket/prefix`: uses `gsutil cp`.
- `rclone:remote:path`: uses `rclone copyto`.

Verify credentials and permissions before deleting local FASTQ caches.
