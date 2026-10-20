"""Free-space preflight: estimate a sample's footprint and refuse to launch
when either external drive lacks room. Internal disk is never used for data."""
from __future__ import annotations

import os
from pathlib import Path

from .util import disk_usage, free_bytes, human_bytes

GB = 1024 ** 3


class SpaceError(RuntimeError):
    pass


def input_bytes_for(sample: dict, cfg) -> int:
    """Total bytes of the sample's input files (.sra or fastq)."""
    total = 0
    for p in sample.get("meta", {}).get("input_paths", []):
        try:
            total += Path(p).stat().st_size
        except OSError:
            pass
    if sample.get("mode", "sra") == "sra":
        # Undownloaded runs still need scratch/archive space. Use the full
        # authoritative run-table footprint, not just the local subset.
        expected = sum(int(size) for size in
                       sample.get("meta", {}).get("run_bytes", {}).values())
        total = max(total, expected)
    return total


def estimate(sample: dict, cfg) -> dict:
    inp = input_bytes_for(sample, cfg)
    mode = sample.get("mode", "sra")
    fq_mult = float(cfg.get("space", "fastq_multiplier", default=2.2))
    conv_mult = float(cfg.get("space", "conversion_multiplier", default=10.0))
    cr_mult = float(cfg.get("space", "cellranger_multiplier", default=2.5))
    if mode == "sra":
        fastq = int(inp * fq_mult)
    else:
        fastq = inp  # already fastq
    cellranger = int(fastq * cr_mult)
    # fasterq temporarily holds uncompressed output and merge chunks at the
    # same time. On the production 151+151 bp data this peaked near 9x the SRA
    # size, substantially above the final fastq.gz footprint.
    conversion_peak = int(inp * conv_mult) if mode == "sra" else 0
    downstream_peak = (fastq if mode == "sra" else 0) + cellranger
    work_needed = max(conversion_peak, downstream_peak)
    # archive: fastq copies (stored zip ~= fastq.gz size) + outs (~0.5x fastq) + loom
    archive_needed = fastq + int(0.6 * fastq)
    return {"input": inp, "fastq_est": fastq,
            "conversion_peak_est": conversion_peak,
            "cellranger_est": cellranger,
            "work_needed": work_needed, "archive_needed": archive_needed}


def same_filesystem(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def preflight(sample: dict, cfg, reserved_work: int = 0,
              reserved_archive: int = 0) -> dict:
    """Raise SpaceError with a clear message when space is insufficient."""
    est = estimate(sample, cfg)
    work_disk = disk_usage(cfg.path("work_root"))
    archive_disk = disk_usage(cfg.path("archive_root"))
    work_free = work_disk["free"]
    archive_free = archive_disk["free"]
    min_work = float(cfg.get("space", "min_free_work_gb", default=100)) * GB
    min_archive = float(cfg.get("space", "min_free_archive_gb", default=50)) * GB

    problems = []
    if work_disk.get("inodes_total", 0) > 0 and \
            work_disk.get("inodes_free", 0) <= 0:
        problems.append(
            "working filesystem reports no free inodes; Cell Ranger requires "
            "a Unix-compatible work filesystem such as APFS or ext4 (not exFAT)")
    shared = same_filesystem(cfg.path("work_root"), cfg.path("archive_root"))
    if shared:
        needed = (reserved_work + reserved_archive
                  + est["work_needed"] + est["archive_needed"])
        reserve = min_work + min_archive
        if min(work_free, archive_free) - needed < reserve:
            problems.append(
                f"shared work/archive SSD: {human_bytes(min(work_free, archive_free))} "
                f"free, active reservations + this sample need ~{human_bytes(needed)} "
                f"plus {human_bytes(reserve)} reserve")
    elif work_free - reserved_work - est["work_needed"] < min_work:
        problems.append(
            f"working SSD: {human_bytes(work_free)} free, needs "
            f"~{human_bytes(reserved_work + est['work_needed'])} including "
            f"active reservations, plus "
            f"{human_bytes(min_work)} reserve")
    if not shared and archive_free - reserved_archive - est["archive_needed"] < min_archive:
        problems.append(
            f"archive drive: {human_bytes(archive_free)} free, needs "
            f"~{human_bytes(reserved_archive + est['archive_needed'])} including "
            f"active reservations, plus "
            f"{human_bytes(min_archive)} reserve")
    if problems:
        raise SpaceError(
            f"insufficient disk space for sample {sample['id']} — "
            + "; ".join(problems)
            + ". Free space or lower space reserves in config.yaml, then retry.")
    return est


def disks_snapshot(cfg) -> dict:
    root = Path(cfg.root.anchor or "/")
    return {
        "internal": disk_usage(root),
        "work": disk_usage(cfg.path("work_root")),
        "archive": disk_usage(cfg.path("archive_root")),
    }
