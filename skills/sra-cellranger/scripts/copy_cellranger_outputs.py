#!/usr/bin/env python3
"""Copy compact Cell Ranger / Cell Ranger ATAC / Cell Ranger ARC outputs into stable per-sample result folders."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


KEEP = [
    "raw_feature_bc_matrix",
    "filtered_feature_bc_matrix",
    "web_summary.html",
    "metrics_summary.csv",
    "molecule_info.h5",
]

# cellranger-atac count. fragments.tsv.gz (+ .tbi) is the essential file: ArchR, Signac
# and SnapATAC2 rebuild their matrices from it. BAMs are left behind.
KEEP_ATAC = [
    "fragments.tsv.gz",
    "fragments.tsv.gz.tbi",
    "filtered_peak_bc_matrix",
    "filtered_peak_bc_matrix.h5",
    "raw_peak_bc_matrix.h5",
    "peaks.bed",
    "peak_annotation.tsv",
    "singlecell.csv",
    "summary.csv",
    "summary.json",
    "web_summary.html",
]

# cellranger-arc count (Multiome ATAC + GEX). BAMs are left behind.
KEEP_MULTIOME = [
    "filtered_feature_bc_matrix",
    "filtered_feature_bc_matrix.h5",
    "raw_feature_bc_matrix.h5",
    "atac_fragments.tsv.gz",
    "atac_fragments.tsv.gz.tbi",
    "atac_peaks.bed",
    "atac_peak_annotation.tsv",
    "per_barcode_metrics.csv",
    "gex_molecule_info.h5",
    "summary.csv",
    "web_summary.html",
]

KEEP_BY_ASSAY = {"rna": KEEP, "atac": KEEP_ATAC, "multiome": KEEP_MULTIOME}


def detect_assay(outs: Path) -> str:
    if (outs / "atac_fragments.tsv.gz").exists():
        return "multiome"
    if (outs / "fragments.tsv.gz").exists():
        return "atac"
    return "rna"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Cell Ranger output base containing <sample>/outs")
    parser.add_argument("--dest", type=Path, required=True, help="Result base where <sample>/outs is written")
    parser.add_argument("--samples", nargs="*", help="Optional sample IDs; defaults to all output dirs with outs")
    parser.add_argument("--assay", choices=["auto", *KEEP_BY_ASSAY], default="auto",
                        help="Which keep-list to use; auto detects ATAC/Multiome from the fragments file")
    return parser.parse_args()


def replace(src: Path, dest: Path) -> None:
    if dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)


def copy_one(run_dir: Path, dest_root: Path, assay: str = "auto") -> None:
    sample = run_dir.name
    outs = run_dir / "outs"
    dest_outs = dest_root / sample / "outs"
    dest_outs.mkdir(parents=True, exist_ok=True)
    if assay == "auto":
        assay = detect_assay(outs)
    print(f"[assay] {sample}: {assay}")
    for item in KEEP_BY_ASSAY[assay]:
        src = outs / item
        if not src.exists():
            print(f"[skip] {sample}/{item} missing")
            continue
        replace(src, dest_outs / item)
        print(f"[copy] {src} -> {dest_outs / item}")


def main() -> int:
    args = parse_args()
    if args.samples:
        run_dirs = [args.source / sample for sample in args.samples]
    else:
        run_dirs = sorted(path for path in args.source.iterdir() if (path / "outs").exists())
    for run_dir in run_dirs:
        if not (run_dir / "outs").exists():
            print(f"[missing] {run_dir / 'outs'}")
            continue
        copy_one(run_dir, args.dest, args.assay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
