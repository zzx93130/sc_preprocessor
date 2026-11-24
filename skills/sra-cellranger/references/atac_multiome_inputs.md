# snATAC-seq and Multiome (snATAC + snRNA) Input Notes

Read this before processing chromatin-accessibility or joint ATAC + GEX data.
Flags below were checked against the Cell Ranger ATAC 2.2 and Cell Ranger ARC
2.2 documentation; run `<tool> count --help` on the installed version before a
long job, because flags differ between releases.

## Pick the pipeline first

| Data | Assay value | Tool | Reference |
| --- | --- | --- | --- |
| snRNA-seq or scRNA-seq only | `rna` | `cellranger count` | `refdata-gex-*` |
| snATAC-seq only | `atac` | `cellranger-atac count` | `refdata-cellranger-arc-*` or `refdata-cellranger-atac-*` |
| 10x Multiome, ATAC and GEX from the same nuclei | `multiome` | `cellranger-arc count` | `refdata-cellranger-arc-*` |

- `cellranger-arc count` needs both modalities and refuses to run one alone.
  If only one Multiome library is available, run it as `rna` or `atac`.
  For the GEX half use `cellranger count --chemistry=ARC-v1`. For the ATAC
  half, some `cellranger-atac` versions take `--chemistry=ARC-v1`; confirm
  with `cellranger-atac count --help`.
- snRNA and snATAC from *different* nuclei of the same tissue (two separate
  kits) are not Multiome. Run them as one `rna` sample and one `atac` sample.
- Decide from GEO/SRA metadata (library strategy, kit name, paired GSMs per
  donor), not from accession names.
- The GEX reference (`refdata-gex-*`) cannot be used for ATAC or ARC.
- 10x lists 64 GB RAM as the minimum for both ATAC and ARC; check the System
  Requirements page for the installed version. Set Docker Desktop memory
  accordingly.

## Sample sheet

Extra optional columns: `assay` (`rna` default, `atac`, `multiome`) and
`library_type` (`Gene Expression` or `Chromatin Accessibility`; `gex` and
`atac` are accepted). `library_type` is required on every `multiome` row.
`read_map` applies to its own row, so the two Multiome libraries can differ.

```csv
accession,sample,assay,library_type,read_map
SRR000010,donor1,multiome,Gene Expression,1=R1;2=R2
SRR000011,donor1,multiome,Chromatin Accessibility,auto
SRR000020,lung_atac_1,atac,,
SRR000021,lung_atac_1,atac,,
SRR000030,nuclei_rna_1,rna,,
```

Write `read_map` with semicolons inside a CSV (`1=R1;2=R2;3=R3`). A comma form
must be quoted, otherwise it spills into the next columns; the helper stops on
such rows.

Several accessions with the same sample and library type are staged as lanes
`L001`, `L002`, … of one library.

## Read layout

10x ATAC FASTQ roles:

| Role | Content | Typical length |
| --- | --- | --- |
| R1 | genomic read 1 | about 50 bp |
| R2 | i5 index read = cell barcode | 16 bp (24 bp for Multiome ATAC) |
| R3 | genomic read 2 | about 50 bp |
| I1 | i7 sample index, optional | 8 bp |

Multiome GEX uses the normal GEX layout: R1 28 bp barcode + UMI, R2 cDNA.

SRA specifics:

- The barcode and index reads are usually flagged technical. `fasterq-dump`
  drops them unless `--include-technical` is given. The helper adds the flag
  for every `atac` and `multiome` sample.
- Common dumps: three files `1=R1,2=R2,3=R3` (the ATAC default), or four files
  with the index first, `1=I1,2=R1,3=R2,4=R3`. Never assume; check lengths:

```bash
python ./scripts/inspect_fastq_reads.py --fastq-dir cache/fastq --assay atac
```

- `read_map=auto` (or `--default-atac-read-map auto`) applies the same length
  rules at run time and stops when the layout is ambiguous.
- If a run has only two long reads even with `--include-technical`, the
  barcode read was not deposited as FASTQ and the run cannot be processed from
  those files. Look in the SRA run's "Data access" tab for the submitter's
  original FASTQs or 10x BAM (convert a BAM with 10x `bamtofastq`), or check
  ENA for the submitted files.

## Staged layout

```
staged_fastqs/lung_atac_1/lung_atac_1_S1_L001_{R1,R2,R3}_001.fastq.gz
staged_fastqs/donor1/gex/donor1_gex_S1_L001_{R1,R2}_001.fastq.gz
staged_fastqs/donor1/atac/donor1_atac_S1_L001_{R1,R2,R3}_001.fastq.gz
staged_fastqs/donor1/libraries.csv
```

The Multiome libraries get `_gex` and `_atac` name suffixes so the two sets
never collide in one zip archive. `libraries.csv` is generated:

```csv
fastqs,sample,library_type
/abs/staged_fastqs/donor1/gex,donor1_gex,Gene Expression
/abs/staged_fastqs/donor1/atac,donor1_atac,Chromatin Accessibility
```

`library_type` is case-sensitive, and `fastqs` must be one absolute directory
per row.

## Commands

```bash
cellranger-atac count --id=lung_atac_1 \
  --reference=/ref/refdata-cellranger-arc-GRCh38-2020-A-2.0.0 \
  --fastqs=/abs/staged_fastqs/lung_atac_1 --sample=lung_atac_1 \
  --localcores=16 --localmem=64

cellranger-arc count --id=donor1 \
  --reference=/ref/refdata-cellranger-arc-GRCh38-2020-A-2.0.0 \
  --libraries=/abs/staged_fastqs/donor1/libraries.csv \
  --create-bam=false --localcores=16 --localmem=64
```

- ARC 2.1 and later require `--create-bam=true|false`. ARC 2.0 has `--no-bam`
  instead; use the helper's `--arc-legacy-bam-flag` there.
- `cellranger-atac count` has no BAM switch; it always writes
  `possorted_bam.bam`.
- Neither tool has `--expect-cells`. Cell-calling overrides
  (`--force-cells` for ATAC; `--min-atac-count` together with
  `--min-gex-count` for ARC) go in `extra_args`, and only after reviewing the
  default web summary.
- ARC counts intronic reads by default, which is right for nuclei;
  `--gex-exclude-introns` turns that off.
- For snRNA-only samples with `cellranger count`, intron counting is the
  default from Cell Ranger 7; on older versions pass `--include-introns`.

## Outputs to keep

`scripts/copy_cellranger_outputs.py` detects the assay from the fragments
file and keeps:

- ATAC: `fragments.tsv.gz` + `.tbi`, `filtered_peak_bc_matrix/` + `.h5`,
  `raw_peak_bc_matrix.h5`, `peaks.bed`, `peak_annotation.tsv`,
  `singlecell.csv`, `summary.csv`, `summary.json`, `web_summary.html`.
- Multiome: `filtered_feature_bc_matrix/` + `.h5`, `raw_feature_bc_matrix.h5`,
  `atac_fragments.tsv.gz` + `.tbi`, `atac_peaks.bed`,
  `atac_peak_annotation.tsv`, `per_barcode_metrics.csv`,
  `gex_molecule_info.h5`, `summary.csv`, `web_summary.html`.

The fragments file with its index is the deliverable that matters most:
ArchR, Signac and SnapATAC2 build their own matrices from it, and without the
`.tbi` it is unusable. BAMs are large and are dropped in compact mode; keep
them (`CR_COPY_MODE=full` or copy by hand) only for a stated downstream need
such as velocyto on `gex_possorted_bam.bam`, footprinting or genotype
demultiplexing.

Completion checks:

- `summary.csv` exists (these tools do not write `metrics_summary.csv`).
- Fragments file and `.tbi` are non-empty; `gzip -t` passes.
- Report from `summary.csv`: estimated cells, median fragments per cell, TSS
  enrichment, fraction of fragments in peaks, valid barcodes; for Multiome
  also median genes and UMIs per cell. Column names differ between versions,
  so read the header instead of hard-coding names.

## Failure patterns

- "Fewer than 10% of read pairs have a valid 10x barcode": wrong read roles
  (barcode read staged as R3, or index staged as R2), or the wrong pipeline
  (`cellranger-atac` on Multiome ATAC or the reverse).
- "No input FASTQs were found": sample name in `libraries.csv` does not match
  the file prefix, or symlink targets are not mounted in Docker.
- Multiome barcodes: the ATAC and GEX gel-bead barcodes are different
  sequences paired by a 10x translation table. ARC reports the GEX barcode
  for both. If the halves were processed separately with `cellranger count`
  and `cellranger-atac count`, translate barcodes before joining cells.
- Low RNA/ATAC cell overlap in ARC usually means the two accessions are not
  from the same GEM well. Recheck the GSM pairing.

## Not covered

- The production queue in `pipeline/scpre` handles these assays through the
  per-sample technology label described in SKILL.md. Its PBS cluster mode and
  FASTQ-mode Multiome are not covered.
- Non-10x ATAC (sci-ATAC, SHARE-seq, Bio-Rad, Parse) needs other tools.
- No loom is produced for Multiome by these helpers.
