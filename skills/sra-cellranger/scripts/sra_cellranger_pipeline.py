#!/usr/bin/env python3
"""Run an SRA FASTQ -> Cell Ranger -> archive/report workflow."""

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


@dataclass
class Sample:
    name: str
    accessions: list[str] = field(default_factory=list)
    read_map: str = "1=R1,2=R2"
    expected_cells: str = ""
    chemistry: str = ""
    extra_args: str = ""


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
    parser.add_argument("--reference", type=Path, required=True, help="Cell Ranger transcriptome reference")
    parser.add_argument("--outdir", type=Path, required=True, help="Project output directory")
    parser.add_argument("--storage", required=True, help="Local path, s3://, gs://, or rclone: destination")
    parser.add_argument("--cellranger", default="cellranger", help="Cell Ranger executable")
    parser.add_argument("--localcores", type=int, default=8)
    parser.add_argument("--localmem", type=int, default=64)
    parser.add_argument("--default-read-map", default="1=R1,2=R2")
    parser.add_argument("--keep-cache", action="store_true", help="Do not remove SRA/FASTQ/intermediate caches")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    return parser.parse_args()


def load_samples(args: argparse.Namespace) -> list[Sample]:
    if args.accessions:
        return [Sample(name=args.sample or args.accessions[0], accessions=args.accessions, read_map=args.default_read_map)]

    grouped: dict[str, Sample] = {}
    with args.sample_sheet.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"accession", "sample"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("sample sheet must include accession and sample columns")
        for row in reader:
            sample_name = clean_id(row["sample"])
            sample = grouped.setdefault(
                sample_name,
                Sample(
                    name=sample_name,
                    read_map=row.get("read_map") or args.default_read_map,
                    expected_cells=row.get("expected_cells", ""),
                    chemistry=row.get("chemistry", ""),
                    extra_args=row.get("extra_args", ""),
                ),
            )
            sample.accessions.append(row["accession"].strip())
    return list(grouped.values())


def clean_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    if not cleaned:
        raise ValueError("empty sample identifier")
    return cleaned


def parse_read_map(value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        left, right = item.split("=", 1)
        role = right.strip().upper()
        if role not in {"R1", "R2", "R3", "I1", "I2"}:
            raise ValueError(f"unsupported read role in read_map: {role}")
        mapping[left.strip()] = role
    return mapping


def require_tools(args: argparse.Namespace) -> None:
    tools = ["prefetch", "fasterq-dump", "gzip", "zip", args.cellranger]
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


def download_accession(accession: str, raw_dir: Path, fastq_dir: Path, runner: Runner) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    fastq_dir.mkdir(parents=True, exist_ok=True)
    runner.run(["prefetch", accession, "-O", str(raw_dir)], log_name=f"prefetch_{accession}.log")
    source = raw_dir / accession / f"{accession}.sra"
    if not source.exists():
        source = raw_dir / accession
    runner.run(
        ["fasterq-dump", "--split-files", "--outdir", str(fastq_dir), str(source)],
        log_name=f"fasterq_{accession}.log",
    )
    for fq in sorted(fastq_dir.glob(f"{accession}_*.fastq")):
        runner.run(["gzip", "-f", str(fq)], log_name=f"gzip_{fq.name}.log")


def stage_fastqs(sample: Sample, fastq_dir: Path, staged_dir: Path, dry_run: bool) -> list[Path]:
    staged_dir.mkdir(parents=True, exist_ok=True)
    read_map = parse_read_map(sample.read_map)
    staged: list[Path] = []
    for lane_index, accession in enumerate(sample.accessions, start=1):
        for read_num, role in read_map.items():
            source = fastq_dir / f"{accession}_{read_num}.fastq.gz"
            target = staged_dir / f"{sample.name}_S1_L{lane_index:03d}_{role}_001.fastq.gz"
            staged.append(target)
            print(f"[stage] {source} -> {target}")
            if not dry_run:
                if not source.exists():
                    raise FileNotFoundError(source)
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(source, target)
    return staged


def run_cellranger(sample: Sample, args: argparse.Namespace, staged_dir: Path, cr_dir: Path, runner: Runner) -> Path:
    cmd = [
        args.cellranger,
        "count",
        f"--id={sample.name}",
        f"--transcriptome={args.reference}",
        f"--fastqs={staged_dir}",
        f"--sample={sample.name}",
        f"--localcores={args.localcores}",
        f"--localmem={args.localmem}",
    ]
    if sample.expected_cells:
        cmd.append(f"--expect-cells={sample.expected_cells}")
    if sample.chemistry:
        cmd.append(f"--chemistry={sample.chemistry}")
    if sample.extra_args:
        cmd.extend(sample.extra_args.split())
    cr_dir.mkdir(parents=True, exist_ok=True)
    runner.run(cmd, log_name=f"cellranger_{sample.name}.log", cwd=cr_dir)
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
    metrics = outs_dir / "metrics_summary.csv"
    if not metrics.exists():
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
        f"- Reference: `{args.reference}`",
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
    require_tools(args)

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
        print(f"\n[sample] {sample.name}")
        for accession in sample.accessions:
            download_accession(accession, raw_dir, fastq_dir, runner)
        staged_dir = staged_base / sample.name
        staged_files = stage_fastqs(sample, fastq_dir, staged_dir, args.dry_run)
        outs_dir = run_cellranger(sample, args, staged_dir, cr_dir, runner)
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
                "accessions": sample.accessions,
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
