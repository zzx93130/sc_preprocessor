#!/usr/bin/env python3
"""Copy compact Cell Ranger outputs into stable per-sample result folders."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Cell Ranger output base containing <sample>/outs")
    parser.add_argument("--dest", type=Path, required=True, help="Result base where <sample>/outs is written")
    parser.add_argument("--samples", nargs="*", help="Optional sample IDs; defaults to all output dirs with outs")
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


def copy_one(run_dir: Path, dest_root: Path) -> None:
    sample = run_dir.name
    outs = run_dir / "outs"
    dest_outs = dest_root / sample / "outs"
    dest_outs.mkdir(parents=True, exist_ok=True)
    for item in KEEP:
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
        copy_one(run_dir, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
