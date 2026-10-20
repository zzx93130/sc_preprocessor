"""Deployment checks for the cluster manager and offline workers."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import psutil

from .artifact_store import load_catalog
from .executors.pbs import PBSExecutor
from .scratch import verify_mount
from .util import disk_usage, writable_dir


def run_preflight(cfg, role: str) -> list[dict]:
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    cluster = cfg.data["cluster"]
    shared_root = Path(cluster["shared_root"])
    require_mount = bool(cluster.get("require_mountpoints", True))
    shared_mount = Path(
        cluster.get("shared_mount") or shared_root.parent
    )
    try:
        verify_mount(
            shared_mount, cluster["shared_identity"],
            require_mountpoint=require_mount,
        )
        add("shared_mount", True, shared_mount)
    except Exception as exc:
        add("shared_mount", False, exc)
    try:
        catalog = load_catalog(Path(cluster["artifacts_catalog"]))
        required = {"cellranger", "reference", "sratools", "crbase", "velocyto"}
        missing = sorted(required - set(catalog["artifacts"]))
        add(
            "artifact_catalog", not missing,
            "complete" if not missing else "missing " + ", ".join(missing),
        )
    except Exception as exc:
        add("artifact_catalog", False, exc)
    add("linux", os.name == "posix", os.name)
    if role == "manager":
        executor = PBSExecutor(cluster["pbs"])
        add("pbs_commands", executor.healthy(), "qsub, qstat, qdel")
        select = str(cluster["pbs"].get("select", ""))
        cores_match = re.search(r"(?:^|:)ncpus=(\d+)", select)
        memory_match = re.search(
            r"(?:^|:)mem=(\d+)(gb|gib)", select, re.IGNORECASE
        )
        requested_cores = int(cfg.get(
            "concurrency", "cellranger_cores", default=0
        ))
        requested_memory = int(cfg.get(
            "concurrency", "cellranger_mem_gb", default=0
        ))
        allocated_cores = int(cores_match.group(1)) if cores_match else 0
        allocated_memory = int(memory_match.group(1)) if memory_match else 0
        profile_ok = (
            allocated_cores >= requested_cores
            and allocated_memory >= requested_memory + 4
        )
        add(
            "pbs_resource_profile", profile_ok,
            f"PBS {allocated_cores} cores/{allocated_memory} GiB; "
            f"Cell Ranger {requested_cores} cores/{requested_memory} GiB",
        )
        add(
            "state_directory", writable_dir(cfg.state_db.parent),
            cfg.state_db.parent,
        )
        add("log_directory", writable_dir(cfg.logs_dir), cfg.logs_dir)
        add(
            "incoming_directory",
            writable_dir(shared_root / "incoming" / "ready"),
            shared_root / "incoming" / "ready",
        )
    elif role == "worker":
        scratch_mount = Path(cluster.get("scratch_mount", "/ssd"))
        try:
            verify_mount(
                scratch_mount, cluster["scratch_identity"],
                require_mountpoint=require_mount,
            )
            add("scratch_mount", True, scratch_mount)
        except Exception as exc:
            add("scratch_mount", False, exc)
        executable = cluster.get("apptainer", {}).get(
            "executable", "apptainer"
        )
        add("apptainer", bool(shutil.which(executable)), executable)
        requested_cores = int(cfg.get(
            "concurrency", "cellranger_cores", default=0
        ))
        requested_memory = int(cfg.get(
            "concurrency", "cellranger_mem_gb", default=0
        ))
        actual_cores = os.cpu_count() or 0
        actual_memory = int(psutil.virtual_memory().total / 1024 ** 3)
        add(
            "worker_resources",
            actual_cores >= requested_cores
            and actual_memory >= requested_memory + 4,
            f"node {actual_cores} cores/{actual_memory} GiB; "
            f"requested {requested_cores} cores/{requested_memory} GiB",
        )
        usage = disk_usage(scratch_mount)
        add(
            "scratch_space", usage.get("ok") and usage.get("free", 0) > 0,
            f"{usage.get('free', 0)} bytes free",
        )
    else:
        raise ValueError("role must be manager or worker")
    return checks
