#!/usr/bin/env python3
"""Stage SRA/ENA FASTQ pairs into Cell Ranger-compatible names."""

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
    return parser.parse_args()


def discover_pairs(source: Path) -> dict[str, tuple[Path, Path]]:
    pairs: dict[str, tuple[Path, Path]] = {}
    for r1 in source.rglob("*_1.fastq.gz"):
        accession = r1.name.removesuffix("_1.fastq.gz")
        r2 = r1.with_name(f"{accession}_2.fastq.gz")
        if r2.exists():
            pairs[accession] = (r1.resolve(), r2.resolve())
    return pairs


def load_groups(sample_sheet: Path | None, pairs: dict[str, tuple[Path, Path]]) -> dict[str, list[str]]:
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


def stage_sample(sample: str, runs: list[str], pairs: dict[str, tuple[Path, Path]], dest_root: Path) -> int:
    sample_dir = dest_root / sample
    sample_dir.mkdir(parents=True, exist_ok=True)
    staged = 0
    for lane, accession in enumerate(runs, start=1):
        r1, r2 = pairs[accession]
        for src, read in ((r1, "R1"), (r2, "R2")):
            dest = sample_dir / f"{sample}_S1_L{lane:03d}_{read}_001.fastq.gz"
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.symlink_to(src)
            staged += 1
            print(f"[stage] {src} -> {dest}")
    return staged


def main() -> int:
    args = parse_args()
    pairs = discover_pairs(args.source)
    groups = load_groups(args.sample_sheet, pairs)
    selected = set(args.samples or groups)
    args.dest.mkdir(parents=True, exist_ok=True)
    for sample in sorted(groups):
        if sample not in selected:
            continue
        count = stage_sample(sample, groups[sample], pairs, args.dest)
        print(f"[staged] {sample}\truns={len(groups[sample])}\tfastqs={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
