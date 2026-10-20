"""OpenPBS/PBS Professional submission and reconciliation."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .base import ExecutorError, JobStatus

SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:-]+$")
SAFE_JOB = re.compile(r"^[A-Za-z0-9_.@-]+$")
STATE_MAP = {
    "Q": "queued", "H": "queued", "W": "queued", "T": "queued",
    "R": "running", "E": "running", "B": "running",
    "F": "finished", "C": "finished", "X": "cancelled",
}


class PBSExecutor:
    def __init__(self, config: dict):
        self.config = config
        self.qsub = str(config.get("qsub", "qsub"))
        self.qstat = str(config.get("qstat", "qstat"))
        self.qdel = str(config.get("qdel", "qdel"))

    def healthy(self) -> bool:
        return all(shutil.which(cmd) for cmd in (self.qsub, self.qstat, self.qdel))

    @staticmethod
    def _safe(value: str, label: str) -> str:
        if not value or not SAFE_VALUE.fullmatch(value):
            raise ExecutorError(f"unsafe PBS {label}: {value!r}")
        return value

    def _call(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True)

    def submit(self, manifest_path: Path, manifest_sha256: str,
               preferred_node: str = "", scratch_bytes: int = 0) -> str:
        manifest = self._safe(str(manifest_path), "manifest path")
        digest = self._safe(manifest_sha256, "manifest checksum")
        script = self._safe(str(self.config["worker_script"]), "worker script")
        select = str(self.config.get("select", "select=1:ncpus=32:mem=128gb"))
        if preferred_node:
            node = self._safe(preferred_node, "node")
            if ":host=" not in select:
                select += f":host={node}"
        command = [self.qsub, "-l", select]
        walltime = str(self.config.get("walltime", "48:00:00"))
        command += ["-l", f"walltime={self._safe(walltime, 'walltime')}"]
        scratch_resource = str(self.config.get("scratch_resource", "")).strip()
        if scratch_resource and scratch_bytes:
            resource = self._safe(scratch_resource, "scratch resource")
            scratch_gb = max(1, math.ceil(scratch_bytes / 1024 ** 3))
            command += ["-l", f"{resource}={scratch_gb}gb"]
        queue = str(self.config.get("queue", "")).strip()
        if queue:
            command += ["-q", self._safe(queue, "queue")]
        variables = (
            f"SCPRE_MANIFEST={manifest},SCPRE_MANIFEST_SHA256={digest}"
        )
        command += ["-v", variables, script]
        result = self._call(command)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise ExecutorError(f"qsub failed: {detail}")
        job_id = (result.stdout or "").strip().split()[0]
        if not SAFE_JOB.fullmatch(job_id):
            raise ExecutorError(f"qsub returned an invalid job id: {job_id!r}")
        return job_id

    def status(self, job_id: str) -> JobStatus | None:
        if not SAFE_JOB.fullmatch(job_id):
            raise ExecutorError(f"invalid PBS job id: {job_id!r}")
        result = self._call([self.qstat, "-f", "-F", "json", job_id])
        if result.returncode:
            text = (result.stderr or result.stdout).lower()
            if "unknown job" in text or "not found" in text:
                return None
            fallback = self._call([self.qstat, "-f", job_id])
            if fallback.returncode:
                text = (fallback.stderr or fallback.stdout).lower()
                if "unknown job" in text or "not found" in text:
                    return None
                raise ExecutorError(text.strip()[-1000:])
            return self._parse_text(job_id, fallback.stdout)
        try:
            document = json.loads(result.stdout)
            jobs = document.get("Jobs", {})
            record = jobs.get(job_id)
            if record is None and jobs:
                record = next(iter(jobs.values()))
            if record is None:
                return None
            return self._status_from_record(job_id, record)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return self._parse_text(job_id, result.stdout)

    @staticmethod
    def _status_from_record(job_id: str, record: dict) -> JobStatus:
        raw = str(record.get("job_state", ""))
        host = str(record.get("exec_host", "")).split("/")[0]
        exit_code = record.get("Exit_status")
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        return JobStatus(
            job_id, STATE_MAP.get(raw, "unknown"), host, exit_code, raw
        )

    def _parse_text(self, job_id: str, output: str) -> JobStatus:
        record = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            record[key.strip()] = value.strip()
        return self._status_from_record(job_id, record)

    def cancel(self, job_id: str) -> None:
        if not SAFE_JOB.fullmatch(job_id):
            raise ExecutorError(f"invalid PBS job id: {job_id!r}")
        result = self._call([self.qdel, job_id])
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise ExecutorError(f"qdel failed for {job_id}: {detail}")
