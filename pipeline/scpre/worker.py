"""Offline PBS worker: local scratch processing and Gluster archive commit."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from .artifact_store import ArtifactCache
from .cluster_archive import ArchiveCommitter, build_bundle
from .cluster_manifest import load_verified
from .config import Config
from .runtimes.apptainer import ApptainerRunner
from .scratch import HeartbeatWriter, verify_mount, write_json_atomic
from .stages import StageContext, run_sample
from .state import State
from .util import disk_usage, rm_rf, sample_logger, sha256_file

WORKER_STAGES = ["downloading", "converting", "cellranger", "loom"]


class WorkerError(RuntimeError):
    pass


def _stage_inputs(document: dict, inbox: Path):
    if document.get("mode", "sra") == "fastq":
        staged = []
        target_dir = inbox / "fastq"
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in document["inputs"]:
            source = Path(item["path"])
            target = target_dir / item["name"]
            if not target.is_file() or sha256_file(target) != item["sha256"]:
                part = target.with_suffix(target.suffix + ".part")
                shutil.copy2(source, part)
                if sha256_file(part) != item["sha256"]:
                    part.unlink(missing_ok=True)
                    raise WorkerError(
                        f"input checksum mismatch for {item['name']}"
                    )
                os.replace(part, target)
            staged.append(str(target))
        return staged
    staged = {}
    for item in document["inputs"]:
        source = Path(item["path"])
        srr = item["srr"]
        target_dir = inbox / srr
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{srr}.sra"
        if not target.is_file() or sha256_file(target) != item["sha256"]:
            part = target.with_suffix(".sra.part")
            shutil.copy2(source, part)
            if sha256_file(part) != item["sha256"]:
                part.unlink(missing_ok=True)
                raise WorkerError(f"input checksum mismatch for {srr}")
            os.replace(part, target)
        staged[srr] = str(target)
    return staged


def _worker_config(document: dict, attempt_root: Path,
                   artifacts: dict[str, Path], inbox: Path) -> Config:
    pipeline = document["pipeline"]
    resources = document["resources"]
    data = {
        "project": document["project"],
        "paths": {
            "cellranger_dir": str(artifacts["cellranger"]),
            "reference_dir": str(artifacts["reference"]),
            "work_root": str(attempt_root),
            "archive_root": str(attempt_root / "unused-archive"),
            "inbox_dir": str(inbox),
        },
        "concurrency": {
            "max_active_samples": 1,
            "max_cellranger_jobs": 1,
            "cellranger_cores": int(resources["cores"]),
            "cellranger_mem_gb": int(resources["memory_gb"]),
            "convert_threads": int(resources.get("convert_threads", 6)),
        },
        "pipeline": {
            "create_bam": bool(pipeline.get("create_bam", True)),
            "make_loom": bool(pipeline.get("make_loom", True)),
            "chemistry": pipeline.get("chemistry", "auto"),
        },
        "metadata": {"read_maps": pipeline.get("read_maps", {})},
        "cleanup": {"remove_inbox": False},
        "tools": {"mode": "apptainer"},
    }
    return Config(data, attempt_root)


def _summaries(ctx: StageContext) -> dict[str, Path]:
    items = {}
    for name in (
        "web_summary.html", "metrics_summary.csv",
        "filtered_feature_bc_matrix.h5", "raw_feature_bc_matrix.h5",
        "molecule_info.h5",
    ):
        source = ctx.outs / name
        if source.exists():
            items[f"results/outs/{name}"] = source
    for name in ("filtered_feature_bc_matrix", "raw_feature_bc_matrix"):
        source = ctx.outs / name
        if source.exists():
            items[f"results/outs/{name}"] = source
    loom = ctx.loom_dir / f"{ctx.sid}.loom"
    if loom.exists():
        items[f"loom/{loom.name}"] = loom
    repro = ctx.work.parent.parent / "repro"
    if repro.is_dir():
        items["repro"] = repro
    worker_log = ctx.work.parent.parent / "worker.log"
    if worker_log.is_file():
        items["logs/worker.log"] = worker_log
    return items


def _write_repro(attempt_root: Path, sample: str, manifest_path: Path,
                 manifest_sha: str, versions: dict) -> Path:
    repro = attempt_root / "repro"
    repro.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest_path, repro / "worker-manifest.json")
    (repro / "versions.json").write_text(
        json.dumps(versions, indent=2, sort_keys=True)
    )
    (repro / "PIPELINE_CODE.txt").write_text(
        "scpre PBS/Apptainer worker manifest schema 1\n"
    )
    script = repro / f"reproduce_{sample}.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "HERE=$(cd \"$(dirname \"$0\")\" && pwd)\n"
        "exec python3 -m scpre.worker "
        "--manifest \"$HERE/worker-manifest.json\" "
        f"--sha256 {manifest_sha}\n"
    )
    script.chmod(0o755)
    return repro


def run_worker(manifest_path: Path, expected_sha256: str) -> int:
    document, manifest_sha = load_verified(manifest_path, expected_sha256)
    sample = document["sample"]
    attempt_id = document["attempt_id"]
    paths = document["paths"]
    shared_root = Path(paths["shared_root"])
    scratch_mount = Path(paths["scratch_mount"])
    scratch_root = Path(paths["scratch_root"])
    require_mount = bool(paths.get("require_mountpoints", True))
    verify_mount(
        Path(paths.get("shared_mount", shared_root.parent)),
        paths["shared_identity"], require_mountpoint=require_mount,
    )
    verify_mount(
        scratch_mount, paths["scratch_identity"],
        require_mountpoint=require_mount,
    )
    scratch_usage = disk_usage(scratch_mount)
    required_scratch = int(document["resources"].get("scratch_bytes", 0))
    reserve_fraction = float(
        document["resources"].get("scratch_min_free_fraction", 0.15)
    )
    reserve = int(scratch_usage.get("total", 0) * reserve_fraction)
    if (not scratch_usage.get("ok")
            or scratch_usage.get("free", 0) < required_scratch + reserve):
        raise WorkerError(
            "local scratch preflight failed: "
            f"{scratch_usage.get('free', 0) / 1024 ** 3:.1f} GiB free, "
            f"{required_scratch / 1024 ** 3:.1f} GiB required plus "
            f"{reserve_fraction:.0%} reserve"
        )
    attempt_root = scratch_root / "jobs" / sample / attempt_id
    attempt_root.mkdir(parents=True, exist_ok=True)
    log_path = attempt_root / "worker.log"
    logger = sample_logger(f"{sample}.{attempt_id}", log_path)
    state = State(attempt_root / ".scpre" / "worker.db")
    inbox = attempt_root / "inbox"
    staged = _stage_inputs(document, inbox)
    cache = ArtifactCache(scratch_root / "cache")
    artifacts = {
        name: cache.materialize(spec)
        for name, spec in document["artifacts"].items()
    }
    required = {"cellranger", "reference", "sratools", "crbase", "velocyto"}
    missing = sorted(required - set(artifacts))
    if missing:
        raise WorkerError("manifest is missing artifacts: " + ", ".join(missing))
    cfg = _worker_config(document, attempt_root, artifacts, inbox)
    runner = ApptainerRunner(
        cfg, artifacts,
        executable=document.get("runtime", {}).get(
            "apptainer", "apptainer"
        ),
    )
    meta = dict(document.get("metadata", {}))
    if document.get("mode", "sra") == "fastq":
        meta["input_paths"] = list(staged)
    else:
        meta["sra_paths"] = staged
        meta["input_paths"] = list(staged.values())
    meta.update(
        attempt_id=attempt_id,
        pbs_job_id=os.environ.get("PBS_JOBID", ""),
        compute_node=socket.gethostname(),
    )
    state.upsert_sample(
        sample, project=document["project"], mode=document.get("mode", "sra"),
        srrs=document.get("srrs", []), status="active",
        stage="staging", meta=meta,
    )
    heartbeat_path = (
        shared_root / "control" / "heartbeats" / f"{attempt_id}.json"
    )

    def heartbeat_document():
        current = state.get_sample(sample) or {}
        return {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "sample": sample,
            "project": document["project"],
            "node": socket.gethostname(),
            "pbs_job_id": os.environ.get("PBS_JOBID", ""),
            "stage": current.get("stage", "starting"),
            "status": current.get("status", "active"),
            "scratch_path": str(attempt_root),
        }

    heartbeat = HeartbeatWriter(
        heartbeat_path, heartbeat_document,
        interval=float(document.get("heartbeat_interval_s", 30)),
    )
    stop = threading.Event()

    def shutdown(_signum, _frame):
        stop.set()
        runner.begin_shutdown()

    old_term = signal.signal(signal.SIGTERM, shutdown)
    old_int = signal.signal(signal.SIGINT, shutdown)
    event_path = shared_root / "control" / "events" / f"{attempt_id}.json"
    heartbeat.start()
    heartbeat.emit()
    try:
        ctx = StageContext(
            cfg, state, runner, state.get_sample(sample), logger
        )
        semaphores = {
            "converting": threading.Semaphore(1),
            "cellranger": threading.Semaphore(1),
        }
        run_sample(
            ctx, semaphores, stop_check=stop.is_set,
            stage_names=WORKER_STAGES,
        )
        state.upsert_sample(sample, status="active", stage="archiving")
        heartbeat.emit()
        repro_dir = _write_repro(
            attempt_root, sample, manifest_path, manifest_sha,
            runner.versions(),
        )
        compression = document.get("archive", {}).get(
            "compression", "zstd"
        )
        suffix = ".tar.zst" if compression == "zstd" \
            else ".tar.gz" if compression == "gz" else ".tar"
        local_bundle = attempt_root / f"{sample}{suffix}"
        sources = {
            "fastq": ctx.fastq_dir,
            "results/outs": ctx.outs,
            "loom": ctx.loom_dir,
            "logs/worker.log": log_path,
            "repro": repro_dir,
        }
        build_bundle(local_bundle, sources, compression)
        committer = ArchiveCommitter(
            Path(paths["archive_root"]), Path(paths["archive_staging_root"])
        )
        final = committer.commit(
            sample, document["project"], attempt_id, manifest_sha,
            local_bundle, compression, _summaries(ctx),
        )
        event = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "sample": sample,
            "project": document["project"],
            "status": "completed",
            "exit_code": 0,
            "node": socket.gethostname(),
            "pbs_job_id": os.environ.get("PBS_JOBID", ""),
            "scratch_path": str(attempt_root),
            "archive_path": str(final),
            "manifest_sha256": manifest_sha,
            "finished_at": time.time(),
        }
        write_json_atomic(event_path, event)
        state.upsert_sample(sample, status="done", stage="done", error="")
        heartbeat.emit()
        if document.get("cleanup", {}).get("remove_scratch_after_commit", True):
            heartbeat.stop()
            heartbeat.join(timeout=5)
            rm_rf(attempt_root)
        return 0
    except Exception as exc:
        logger.exception("worker failed: %s", exc)
        current = state.get_sample(sample) or {}
        write_json_atomic(event_path, {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "sample": sample,
            "project": document["project"],
            "status": "failed",
            "exit_code": 1,
            "node": socket.gethostname(),
            "pbs_job_id": os.environ.get("PBS_JOBID", ""),
            "stage": current.get("stage", "unknown"),
            "scratch_path": str(attempt_root),
            "manifest_sha256": manifest_sha,
            "error": str(exc)[:4000],
            "finished_at": time.time(),
        })
        heartbeat.emit()
        return 1
    finally:
        heartbeat.stop()
        heartbeat.join(timeout=5)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="scpre-worker")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    sys.exit(run_worker(Path(args.manifest), args.sha256))


if __name__ == "__main__":
    main()
