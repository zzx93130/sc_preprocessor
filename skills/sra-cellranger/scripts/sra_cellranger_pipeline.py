#!/usr/bin/env python3
"""Run an SRA FASTQ -> Cell Ranger -> archive/report workflow.

Supports three assays per sample:
  rna       cellranger count        (sc/snRNA-seq gene expression)
  atac      cellranger-atac count   (sc/snATAC-seq)
  multiome  cellranger-arc count    (joint snATAC + snRNA from the same nuclei)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


GEX = "Gene Expression"
ATAC = "Chromatin Accessibility"
ASSAYS = ("rna", "atac", "multiome")
LIBRARY_ALIASES = {
    "gene expression": GEX, "gex": GEX, "rna": GEX, "snrna": GEX, "scrna": GEX,
    "chromatin accessibility": ATAC, "atac": ATAC, "snatac": ATAC, "scatac": ATAC,
}
ASSAY_ALIASES = {
    "": "", "rna": "rna", "gex": "rna", "snrna": "rna", "scrna": "rna", "snrna-seq": "rna", "scrna-seq": "rna",
    "atac": "atac", "snatac": "atac", "scatac": "atac", "snatac-seq": "atac", "scatac-seq": "atac",
    "multiome": "multiome", "arc": "multiome", "atac+gex": "multiome", "gex+atac": "multiome",
}
# Short tag used in staged directory and FASTQ names of Multiome libraries.
LIBRARY_TAG = {GEX: "gex", ATAC: "atac"}


@dataclass
class Library:
    accession: str
    library_type: str = GEX
    read_map: str = ""  # "" = assay default, "auto" = infer from read lengths


@dataclass
class Sample:
    name: str
    assay: str = "rna"
    libraries: list[Library] = field(default_factory=list)
    expected_cells: str = ""
    chemistry: str = ""
    extra_args: str = ""

    @property
    def accessions(self) -> list[str]:
        return [lib.accession for lib in self.libraries]


class Runner:
    def __init__(self, dry_run: bool, log_dir: Path):
        self.dry_run = dry_run
        self.log_dir = log_dir
        self.commands: list[str] = []

    def run(self, cmd: list[str], log_name: str | None = None, cwd: Path | None = None) -> None:
        pretty = " ".join(cmd)
        self.commands.append(pretty)
        print(f"[cmd] {pretty}")
        if self.dry_run:
            return
        stdout = subprocess.PIPE
        stderr = subprocess.STDOUT
        if log_name:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / log_name).open("w", encoding="utf-8") as log:
                proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=log, stderr=subprocess.STDOUT)
        else:
            proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=stdout, stderr=stderr)
            if proc.stdout:
                print(proc.stdout)
        if proc.returncode:
            raise RuntimeError(f"command failed ({proc.returncode}): {pretty}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download SRA FASTQs, run Cell Ranger, archive raw reads, clean caches, and report."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--accessions", nargs="+", help="SRA/SRR accessions for a single sample")
    src.add_argument("--sample-sheet", type=Path, help="CSV with accession,sample columns")
    parser.add_argument("--sample", help="Sample name when using --accessions")
    parser.add_argument("--assay", choices=ASSAYS, default="rna",
                        help="Assay for --accessions, and the default for sample-sheet rows without an assay column")
    parser.add_argument("--reference", type=Path, help="Cell Ranger transcriptome reference (required for rna samples)")
    parser.add_argument("--atac-reference", type=Path,
                        help="Cell Ranger ARC/ATAC reference, e.g. refdata-cellranger-arc-GRCh38-2020-A-2.0.0 "
                             "(required for atac and multiome samples)")
    parser.add_argument("--outdir", type=Path, required=True, help="Project output directory")
    parser.add_argument("--storage", required=True, help="Local path, s3://, gs://, or rclone: destination")
    parser.add_argument("--cellranger", default="cellranger", help="Cell Ranger executable")
    parser.add_argument("--cellranger-atac", default="cellranger-atac", help="Cell Ranger ATAC executable")
    parser.add_argument("--cellranger-arc", default="cellranger-arc", help="Cell Ranger ARC executable")
    parser.add_argument("--create-bam", choices=("true", "false"),
                        help="rna: passed to cellranger count only when set. multiome: passed to "
                             "cellranger-arc count, default false. cellranger-atac always writes a BAM.")
    parser.add_argument("--arc-legacy-bam-flag", action="store_true",
                        help="Cell Ranger ARC 2.0 has no --create-bam; use --no-bam instead when BAMs are not wanted")
    parser.add_argument("--include-technical", action="store_true",
                        help="Pass --include-technical to fasterq-dump for rna samples too "
                             "(always on for atac and multiome, whose barcode reads are usually technical)")
    parser.add_argument("--localcores", type=int, default=8)
    parser.add_argument("--localmem", type=int, default=64)
    parser.add_argument("--default-read-map", default="1=R1,2=R2", help="Read map for Gene Expression libraries")
    parser.add_argument("--default-atac-read-map", default="1=R1,2=R2,3=R3",
                        help="Read map for Chromatin Accessibility libraries; use 'auto' to infer from read lengths")
    parser.add_argument("--keep-cache", action="store_true", help="Do not remove SRA/FASTQ/intermediate caches")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    return parser.parse_args()


def load_samples(args: argparse.Namespace) -> list[Sample]:
    if args.accessions:
        if args.assay == "multiome":
            raise ValueError("multiome needs a sample sheet with a library_type column; --accessions cannot label libraries")
        library_type = ATAC if args.assay == "atac" else GEX
        sample = Sample(
            name=clean_id(args.sample or args.accessions[0]),
            assay=args.assay,
            libraries=[Library(acc, library_type) for acc in args.accessions],
        )
        return validate_samples([sample])

    grouped: dict[str, Sample] = {}
    with args.sample_sheet.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"accession", "sample"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("sample sheet must include accession and sample columns")
        for row in reader:
            if None in row:
                raise ValueError(
                    f"sample sheet row for {row.get('accession')} has more fields than the header; "
                    "write read_map with semicolons (1=R1;2=R2;3=R3) or quote it"
                )
            sample_name = clean_id(row["sample"])
            raw_assay = (row.get("assay") or "").strip().lower()
            if raw_assay not in ASSAY_ALIASES:
                raise ValueError(f"{sample_name}: unsupported assay '{raw_assay}' (use rna, atac, or multiome)")
            assay = ASSAY_ALIASES[raw_assay] or args.assay
            sample = grouped.setdefault(
                sample_name,
                Sample(
                    name=sample_name,
                    assay=assay,
                    expected_cells=(row.get("expected_cells") or "").strip(),
                    chemistry=(row.get("chemistry") or "").strip(),
                    extra_args=(row.get("extra_args") or "").strip(),
                ),
            )
            if sample.assay != assay:
                raise ValueError(f"{sample_name}: rows disagree on assay ({sample.assay} vs {assay})")
            raw_type = (row.get("library_type") or "").strip().lower()
            if raw_type and raw_type not in LIBRARY_ALIASES:
                raise ValueError(f"{sample_name}: unsupported library_type '{raw_type}'")
            if raw_type:
                library_type = LIBRARY_ALIASES[raw_type]
            elif assay == "multiome":
                raise ValueError(
                    f"{sample_name}: multiome rows need library_type "
                    f"('{GEX}' or '{ATAC}') for accession {row['accession'].strip()}"
                )
            else:
                library_type = ATAC if assay == "atac" else GEX
            sample.libraries.append(
                Library(row["accession"].strip(), library_type, (row.get("read_map") or "").strip())
            )
    return validate_samples(list(grouped.values()))


def validate_samples(samples: list[Sample]) -> list[Sample]:
    for sample in samples:
        types = {lib.library_type for lib in sample.libraries}
        if sample.assay == "rna" and types != {GEX}:
            raise ValueError(f"{sample.name}: rna samples take only '{GEX}' libraries")
        if sample.assay == "atac" and types != {ATAC}:
            raise ValueError(f"{sample.name}: atac samples take only '{ATAC}' libraries")
        if sample.assay == "multiome" and types != {GEX, ATAC}:
            raise ValueError(
                f"{sample.name}: multiome needs at least one '{GEX}' and one '{ATAC}' accession from the same "
                "nuclei. cellranger-arc cannot run one modality alone; use assay rna or atac for that."
            )
        if len(set(sample.accessions)) != len(sample.accessions):
            raise ValueError(f"{sample.name}: duplicate accession in sample sheet")
    return samples


def clean_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    if not cleaned:
        raise ValueError("empty sample identifier")
    return cleaned


def parse_read_map(value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in value.replace(";", ",").split(","):
        if not item.strip():
            continue
        left, right = item.split("=", 1)
        role = right.strip().upper()
        if role not in {"R1", "R2", "R3", "I1", "I2"}:
            raise ValueError(f"unsupported read role in read_map: {role}")
        mapping[left.strip()] = role
    return mapping


def require_tools(args: argparse.Namespace, samples: list[Sample]) -> None:
    assays = {sample.assay for sample in samples}
    if "rna" in assays and args.reference is None:
        raise ValueError("--reference is required for rna samples")
    if assays & {"atac", "multiome"} and args.atac_reference is None:
        raise ValueError("--atac-reference is required for atac and multiome samples")
    tools = ["prefetch", "fasterq-dump", "gzip", "zip"]
    tools += [exe for assay, exe in (("rna", args.cellranger), ("atac", args.cellranger_atac),
                                     ("multiome", args.cellranger_arc)) if assay in assays]
    if args.storage.startswith("s3://"):
        tools.append("aws")
    elif args.storage.startswith("gs://"):
        tools.append("gsutil")
    elif args.storage.startswith("rclone:"):
        tools.append("rclone")
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing and not args.dry_run:
        raise RuntimeError(f"missing required executable(s): {', '.join(missing)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_accession(accession: str, raw_dir: Path, fastq_dir: Path, runner: Runner,
                       include_technical: bool = False) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    fastq_dir.mkdir(parents=True, exist_ok=True)
    runner.run(["prefetch", accession, "-O", str(raw_dir)], log_name=f"prefetch_{accession}.log")
    source = raw_dir / accession / f"{accession}.sra"
    if not source.exists():
        source = raw_dir / accession
    dump = ["fasterq-dump", "--split-files"]
    if include_technical:
        # 10x barcode/index reads are often flagged technical and are dropped without this.
        dump.append("--include-technical")
    runner.run([*dump, "--outdir", str(fastq_dir), str(source)], log_name=f"fasterq_{accession}.log")
    for fq in sorted(fastq_dir.glob(f"{accession}_*.fastq")):
        runner.run(["gzip", "-f", str(fq)], log_name=f"gzip_{fq.name}.log")


def resolve_read_map(library: Library, args: argparse.Namespace, fastq_dir: Path, dry_run: bool) -> dict[str, str]:
    default = args.default_atac_read_map if library.library_type == ATAC else args.default_read_map
    value = library.read_map or default
    if value.lower() != "auto":
        return parse_read_map(value)
    fallback = "1=R1,2=R2,3=R3" if library.library_type == ATAC else "1=R1,2=R2"
    if dry_run:
        print(f"[stage] {library.accession}: read_map=auto is resolved from read lengths at run time; showing {fallback}")
        return parse_read_map(fallback)
    from inspect_fastq_reads import accession_lengths, format_read_map, infer_read_map

    lengths = accession_lengths(fastq_dir, library.accession)
    mapping = infer_read_map(lengths, "atac" if library.library_type == ATAC else "rna")
    print(f"[stage] {library.accession}: read lengths {lengths} -> read_map {format_read_map(mapping)}")
    return mapping


def stage_fastqs(
    sample: Sample, args: argparse.Namespace, fastq_dir: Path, staged_dir: Path, dry_run: bool
) -> tuple[list[Path], Path | None]:
    """Symlink split FASTQs under Cell Ranger names.

    rna/atac: staged_dir/<sample>_S1_L00n_<role>_001.fastq.gz
    multiome: staged_dir/{gex,atac}/<sample>_{gex,atac}_S1_L00n_<role>_001.fastq.gz plus
              staged_dir/libraries.csv, which is returned as the second value.
    """
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    lanes: dict[str, int] = {}
    library_rows: dict[str, tuple[Path, str]] = {}
    for library in sample.libraries:
        tag = LIBRARY_TAG[library.library_type]
        if sample.assay == "multiome":
            target_dir, prefix = staged_dir / tag, f"{sample.name}_{tag}"
        else:
            target_dir, prefix = staged_dir, sample.name
        target_dir.mkdir(parents=True, exist_ok=True)
        library_rows[library.library_type] = (target_dir, prefix)
        lanes[tag] = lanes.get(tag, 0) + 1
        read_map = resolve_read_map(library, args, fastq_dir, dry_run)
        roles = set(read_map.values())
        needed = {"R1", "R2", "R3"} if library.library_type == ATAC else {"R1", "R2"}
        if not needed.issubset(roles):
            raise ValueError(
                f"{sample.name}/{library.accession}: {library.library_type} needs roles "
                f"{sorted(needed)}, read_map gives {sorted(roles)}"
            )
        for read_num, role in read_map.items():
            source = fastq_dir / f"{library.accession}_{read_num}.fastq.gz"
            target = target_dir / f"{prefix}_S1_L{lanes[tag]:03d}_{role}_001.fastq.gz"
            staged.append(target)
            print(f"[stage] {source} -> {target}")
            if not dry_run:
                if not source.exists():
                    raise FileNotFoundError(
                        f"{source} (if a barcode/index read is missing, check that fasterq-dump ran with "
                        "--include-technical and that the submitter deposited it)"
                    )
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(source, target)

    libraries_csv: Path | None = None
    if sample.assay == "multiome":
        libraries_csv = staged_dir / "libraries.csv"
        lines = ["fastqs,sample,library_type"]
        lines += [f"{library_rows[kind][0]},{library_rows[kind][1]},{kind}" for kind in (GEX, ATAC)]
        print(f"[stage] libraries.csv -> {libraries_csv}")
        for line in lines:
            print(f"[stage]   {line}")
        if not dry_run:
            libraries_csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return staged, libraries_csv


def run_cellranger(
    sample: Sample,
    args: argparse.Namespace,
    staged_dir: Path,
    cr_dir: Path,
    runner: Runner,
    libraries_csv: Path | None = None,
) -> Path:
    resources = [f"--localcores={args.localcores}", f"--localmem={args.localmem}"]
    if sample.assay == "multiome":
        cmd = [
            args.cellranger_arc,
            "count",
            f"--id={sample.name}",
            f"--reference={args.atac_reference}",
            f"--libraries={libraries_csv}",
            *resources,
        ]
        if args.arc_legacy_bam_flag:
            if args.create_bam != "true":
                cmd.append("--no-bam")
        else:
            cmd.append(f"--create-bam={args.create_bam or 'false'}")
    elif sample.assay == "atac":
        cmd = [
            args.cellranger_atac,
            "count",
            f"--id={sample.name}",
            f"--reference={args.atac_reference}",
            f"--fastqs={staged_dir}",
            f"--sample={sample.name}",
            *resources,
        ]
    else:
        cmd = [
            args.cellranger,
            "count",
            f"--id={sample.name}",
            f"--transcriptome={args.reference}",
            f"--fastqs={staged_dir}",
            f"--sample={sample.name}",
            *resources,
        ]
        if args.create_bam:
            cmd.append(f"--create-bam={args.create_bam}")
        if sample.expected_cells:
            cmd.append(f"--expect-cells={sample.expected_cells}")
    if sample.assay != "rna" and sample.expected_cells:
        print(f"[warn] {sample.name}: expected_cells is ignored for {sample.assay}; "
              "use extra_args (--force-cells, or --min-atac-count with --min-gex-count) after reviewing the web summary")
    if sample.chemistry and sample.assay != "multiome":
        cmd.append(f"--chemistry={sample.chemistry}")
    if sample.extra_args:
        cmd.extend(sample.extra_args.split())
    cr_dir.mkdir(parents=True, exist_ok=True)
    runner.run(cmd, log_name=f"{Path(cmd[0]).name}_{sample.name}.log", cwd=cr_dir)
    return cr_dir / sample.name / "outs"


def preserve_results(sample: Sample, outs_dir: Path, results_base: Path, dry_run: bool) -> Path:
    result_dir = results_base / sample.name / "outs"
    print(f"[results] {outs_dir} -> {result_dir}")
    if not dry_run:
        if not outs_dir.exists():
            raise FileNotFoundError(f"Cell Ranger outs directory not found: {outs_dir}")
        if result_dir.exists():
            shutil.rmtree(result_dir)
        result_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(outs_dir, result_dir)
    return result_dir


def archive_fastqs(sample: Sample, staged_files: list[Path], archive_dir: Path, runner: Runner) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{sample.name}_raw_fastqs.zip"
    runner.run(["zip", "-j", str(archive), *[str(path) for path in staged_files]], log_name=f"zip_{sample.name}.log")
    return archive


def upload_archive(archive: Path, storage: str, runner: Runner) -> str:
    if storage.startswith("s3://"):
        dest = f"{storage.rstrip('/')}/{archive.name}"
        runner.run(["aws", "s3", "cp", str(archive), dest], log_name=f"upload_{archive.stem}.log")
        return dest
    if storage.startswith("gs://"):
        dest = f"{storage.rstrip('/')}/{archive.name}"
        runner.run(["gsutil", "cp", str(archive), dest], log_name=f"upload_{archive.stem}.log")
        return dest
    if storage.startswith("rclone:"):
        dest = f"{storage.rstrip('/')}/{archive.name}"
        runner.run(["rclone", "copyto", str(archive), dest], log_name=f"upload_{archive.stem}.log")
        return dest
    dest_path = Path(storage).expanduser() / archive.name
    print(f"[copy] {archive} -> {dest_path}")
    if not runner.dry_run:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, dest_path)
    return str(dest_path)


def collect_metrics(outs_dir: Path) -> dict[str, str]:
    # cellranger count writes metrics_summary.csv; cellranger-atac and cellranger-arc write summary.csv.
    metrics = next((outs_dir / name for name in ("metrics_summary.csv", "summary.csv") if (outs_dir / name).exists()), None)
    if metrics is None:
        return {}
    with metrics.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        return {}
    return dict(zip(rows[0], rows[1]))


def cleanup(sample: Sample, args: argparse.Namespace, paths: list[Path], runner: Runner) -> list[str]:
    removed: list[str] = []
    if args.keep_cache:
        return removed
    for path in paths:
        if runner.dry_run:
            print(f"[cleanup] would remove {path}")
            removed.append(str(path))
        elif path.exists():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(str(path))
    return removed


def write_report(args: argparse.Namespace, sample_reports: list[dict[str, object]], runner: Runner) -> Path:
    report_dir = args.outdir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "sra_cellranger_summary.md"
    lines = [
        "# SRA Cell Ranger Summary",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Output directory: `{args.outdir}`",
        f"- GEX reference: `{args.reference}`",
        f"- ATAC/ARC reference: `{args.atac_reference}`",
        f"- Storage: `{args.storage}`",
        f"- Dry run: `{args.dry_run}`",
        "",
        "## Samples",
        "",
    ]
    for item in sample_reports:
        lines.extend(
            [
                f"### {item['sample']}",
                "",
                f"- Assay: {item['assay']}",
                f"- Accessions: {', '.join(item['accessions'])}",
                f"- Results: `{item['outs_dir']}`",
                f"- FASTQ archive: `{item['archive']}`",
                f"- Uploaded to: `{item['upload_dest']}`",
                f"- Archive SHA256: `{item['sha256']}`",
                f"- Cleanup removed: {', '.join(item['cleanup']) or 'nothing'}",
                "",
            ]
        )
        metrics = item.get("metrics", {})
        if metrics:
            lines.append("| Metric | Value |")
            lines.append("| --- | --- |")
            for key, value in metrics.items():
                lines.append(f"| {key} | {value} |")
            lines.append("")
    lines.extend(["## Commands", ""])
    lines.extend(f"- `{cmd}`" for cmd in runner.commands)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    args = parse_args()
    args.outdir = args.outdir.expanduser().resolve()
    samples = load_samples(args)
    require_tools(args, samples)

    raw_dir = args.outdir / "cache" / "sra"
    fastq_dir = args.outdir / "cache" / "fastq"
    staged_base = args.outdir / "staged_fastqs"
    cr_dir = args.outdir / "cellranger_work"
    results_base = args.outdir / "results"
    archive_dir = args.outdir / "archives"
    log_dir = args.outdir / "logs"
    for path in [raw_dir, fastq_dir, staged_base, cr_dir, results_base, archive_dir, log_dir]:
        path.mkdir(parents=True, exist_ok=True)

    runner = Runner(args.dry_run, log_dir)
    sample_reports: list[dict[str, object]] = []

    for sample in samples:
        print(f"\n[sample] {sample.name} assay={sample.assay}")
        include_technical = args.include_technical or sample.assay != "rna"
        for accession in sample.accessions:
            download_accession(accession, raw_dir, fastq_dir, runner, include_technical)
        staged_dir = staged_base / sample.name
        staged_files, libraries_csv = stage_fastqs(sample, args, fastq_dir, staged_dir, args.dry_run)
        outs_dir = run_cellranger(sample, args, staged_dir, cr_dir, runner, libraries_csv)
        result_outs = preserve_results(sample, outs_dir, results_base, args.dry_run)
        archive = archive_fastqs(sample, staged_files, archive_dir, runner)
        upload_dest = upload_archive(archive, args.storage, runner)
        digest = "dry-run"
        if not args.dry_run and archive.exists():
            digest = sha256(archive)
        metrics = collect_metrics(result_outs) if not args.dry_run else {}
        removed = cleanup(sample, args, [raw_dir, fastq_dir, staged_dir, cr_dir / sample.name], runner)
        sample_reports.append(
            {
                "sample": sample.name,
                "assay": sample.assay,
                "accessions": [f"{lib.accession} ({lib.library_type})" for lib in sample.libraries],
                "outs_dir": str(result_outs),
                "archive": str(archive),
                "upload_dest": upload_dest,
                "sha256": digest,
                "cleanup": removed,
                "metrics": metrics,
            }
        )

    report = write_report(args, sample_reports, runner)
    print(f"\n[report] {report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(textwrap.dedent(f"""
        ERROR: {exc}
        The run stopped before completion. Check logs in the selected output directory.
        """).strip(), file=sys.stderr)
        raise SystemExit(1)
