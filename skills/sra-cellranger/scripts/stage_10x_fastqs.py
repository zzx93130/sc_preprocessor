#!/usr/bin/env python3
"""Stage SRA/ENA split FASTQs into Cell Ranger-compatible names.

Default: {SRR}_1/_2.fastq.gz -> R1/R2 (gene expression).
ATAC:    --read-map 1=R1,2=R2,3=R3 (or 1=I1,2=R1,3=R2,4=R3 when an index read was dumped).
Multiome: run once per library with --sample-suffix _gex / _atac into separate --dest folders.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="FASTQ root containing SRR*_1/2.fastq.gz files")
    parser.add_argument("--dest", type=Path, required=True, help="Cell Ranger staged FASTQ root")
    parser.add_argument("--sample-sheet", type=Path, help="CSV with accession,sample columns")
    parser.add_argument("--samples", nargs="*", help="Optional sample IDs/accessions to stage")
    parser.add_argument("--read-map", default="1=R1,2=R2",
                        help="SRA read number to Cell Ranger role, e.g. 1=R1,2=R2,3=R3 for 10x ATAC")
    parser.add_argument("--sample-suffix", default="",
                        help="Appended to the sample name in staged FASTQ names, e.g. _atac for a Multiome ATAC library")
    return parser.parse_args()


def parse_read_map(value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in value.replace(";", ",").split(","):
        if not item.strip():
            continue
        number, role = (part.strip() for part in item.split("=", 1))
        role = role.upper()
        if role not in {"R1", "R2", "R3", "I1", "I2"}:
            raise ValueError(f"unsupported read role in read map: {role}")
        mapping[number] = role
    if "1" not in mapping:
        raise ValueError("read map must include SRA read 1")
    return mapping


def discover_pairs(source: Path, read_map: dict[str, str] | None = None) -> dict[str, tuple[tuple[Path, str], ...]]:
    """Return {accession: ((path, role), ...)} for accessions with every mapped read present."""
    read_map = read_map or {"1": "R1", "2": "R2"}
    pairs: dict[str, tuple[tuple[Path, str], ...]] = {}
    for r1 in source.rglob("*_1.fastq.gz"):
        accession = r1.name.removesuffix("_1.fastq.gz")
        files = [(r1.with_name(f"{accession}_{number}.fastq.gz"), role) for number, role in read_map.items()]
        missing = [path.name for path, _ in files if not path.exists()]
        if missing:
            if len(read_map) > 2:
                print(f"[skip] {accession}: missing {', '.join(missing)}")
            continue
        pairs[accession] = tuple((path.resolve(), role) for path, role in files)
    return pairs


def load_groups(sample_sheet: Path | None, pairs: dict) -> dict[str, list[str]]:
    if sample_sheet is None:
        return {accession: [accession] for accession in sorted(pairs)}
    groups: dict[str, list[str]] = defaultdict(list)
    with sample_sheet.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not {"accession", "sample"}.issubset(reader.fieldnames or []):
            raise ValueError("sample sheet must include accession and sample columns")
        for row in reader:
            accession = row["accession"].strip()
            sample = row["sample"].strip()
            if accession in pairs and sample:
                groups[sample].append(accession)
    return {sample: runs for sample, runs in groups.items() if runs}


def stage_sample(sample: str, runs: list[str], pairs: dict, dest_root: Path, suffix: str = "") -> int:
    sample_dir = dest_root / sample
    sample_dir.mkdir(parents=True, exist_ok=True)
    staged = 0
    for lane, accession in enumerate(runs, start=1):
        for src, read in pairs[accession]:
            dest = sample_dir / f"{sample}{suffix}_S1_L{lane:03d}_{read}_001.fastq.gz"
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.symlink_to(src)
            staged += 1
            print(f"[stage] {src} -> {dest}")
    return staged


def main() -> int:
    args = parse_args()
    pairs = discover_pairs(args.source, parse_read_map(args.read_map))
    groups = load_groups(args.sample_sheet, pairs)
    selected = set(args.samples or groups)
    args.dest.mkdir(parents=True, exist_ok=True)
    for sample in sorted(groups):
        if sample not in selected:
            continue
        count = stage_sample(sample, groups[sample], pairs, args.dest, args.sample_suffix)
        print(f"[staged] {sample}\truns={len(groups[sample])}\tfastqs={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
