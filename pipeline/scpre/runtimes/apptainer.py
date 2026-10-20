"""Apptainer implementation of the existing stage-runner contract."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..docker_run import ToolError
from ..util import run_cmd


class ApptainerRunner:
    def __init__(self, cfg, artifacts: dict[str, Path],
                 executable: str = "apptainer"):
        self.cfg = cfg
        self.artifacts = artifacts
        self.executable = executable
        self._shutdown = threading.Event()

    def healthy(self) -> bool:
        if not shutil.which(self.executable):
            return False
        result = subprocess.run(
            [self.executable, "--version"], capture_output=True, text=True
        )
        return result.returncode == 0

    def mounts_healthy(self, paths) -> str:
        missing = [str(path) for path in paths if not Path(path).exists()]
        return "missing paths: " + ", ".join(missing) if missing else ""

    def begin_shutdown(self):
        self._shutdown.set()

    def deferred_cleanup_requires_restart(self):
        return False

    def running_containers(self):
        return []

    def restart_desktop_for_cleanup(self):
        return None

    def list_jobs(self):
        return []

    def sample_jobs(self, sample):
        return []

    def assert_sample_idle(self, sample):
        if self._shutdown.is_set():
            raise ToolError("worker is stopping")

    def stop_sample_jobs(self, sample, logger=None, timeout_s=20):
        self._shutdown.set()
        return []

    def _exec(self, image_key: str, mounts, argv, logger, workdir="",
              env=None):
        if self._shutdown.is_set():
            raise ToolError("worker is stopping")
        image = self.artifacts[image_key]
        command = [self.executable, "exec", "--cleanenv", "--containall"]
        for host, container, read_only in mounts:
            suffix = ":ro" if read_only else ":rw"
            command += ["--bind", f"{host}:{container}{suffix}"]
        if workdir:
            command += ["--pwd", workdir]
        for key, value in (env or {}).items():
            command += ["--env", f"{key}={value}"]
        command += [str(image)] + [str(arg) for arg in argv]
        result = run_cmd(command, logger=logger)
        if result.returncode:
            tail = "\n".join(result.stdout.splitlines()[-20:])
            raise ToolError(
                f"{argv[0]} failed (exit {result.returncode}):\n{tail}"
            )
        return result

    def prefetch(self, srr, dest_dir, sample, logger):
        raise ToolError(
            f"offline worker cannot download {srr}; manager input is incomplete"
        )

    def fasterq(self, sra_path: Path, out_dir: Path, tmp_dir: Path,
                threads: int, sample: str, logger):
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        result = self._exec(
            "sratools",
            [(sra_path.parent, "/in", True), (out_dir, "/out", False),
             (tmp_dir, "/tmp_fq", False)],
            ["fasterq-dump", "--split-files", "--include-technical",
             "-e", str(threads), "-t", "/tmp_fq", "-O", "/out",
             f"/in/{sra_path.name}"],
            logger,
        )
        produced = sorted(out_dir.glob(f"{sra_path.stem}_*.fastq"))
        if not produced:
            raise ToolError(f"fasterq-dump produced no FASTQ for {sra_path}")
        spots = None
        for line in result.stdout.splitlines():
            if "spots read" in line:
                digits = "".join(ch for ch in line.split(":")[-1] if ch.isdigit())
                if digits:
                    spots = int(digits)
        return produced, spots

    def compress(self, paths, threads: int, sample: str, logger):
        output = []
        for path in paths:
            self._exec(
                "sratools", [(path.parent, "/data", False)],
                ["pigz", "-f", "-p", str(threads), f"/data/{path.name}"],
                logger,
            )
            output.append(path.with_name(path.name + ".gz"))
        return output

    def cellranger_count(self, sample: str, fastq_dir: Path, run_dir: Path,
                         ref_dir: Path, cores: int, mem_gb: int,
                         create_bam: bool, logger):
        run_dir.mkdir(parents=True, exist_ok=True)
        temporary = run_dir / "tmp"
        temporary.mkdir(exist_ok=True)
        cellranger = self.artifacts["cellranger"]
        self._exec(
            "crbase",
            [(cellranger, "/opt/cellranger", True), (ref_dir, "/ref", True),
             (fastq_dir, "/fastq", True), (run_dir, "/work", False)],
            ["/opt/cellranger/cellranger", "count", f"--id={sample}",
             "--fastqs=/fastq", f"--sample={sample}", "--transcriptome=/ref",
             f"--localcores={cores}", f"--localmem={mem_gb}",
             f"--create-bam={'true' if create_bam else 'false'}",
             "--disable-ui"],
            logger, workdir="/work", env={"TMPDIR": "/work/tmp"},
        )
        outs = run_dir / sample / "outs"
        if not outs.is_dir():
            raise ToolError(f"Cell Ranger outputs are missing: {outs}")
        return outs

    def velocyto(self, sample: str, outs_dir: Path, gtf_path: Path,
                 loom_dir: Path, threads: int, logger):
        loom_dir.mkdir(parents=True, exist_ok=True)
        self._exec(
            "velocyto",
            [(outs_dir, "/outs", False), (gtf_path.parent, "/gtf", True),
             (loom_dir, "/loom", False)],
            ["velocyto", "run",
             "-b", "/outs/filtered_feature_bc_matrix/barcodes.tsv.gz",
             "-o", "/loom", "-e", sample, "-@", str(threads),
             "/outs/possorted_genome_bam.bam", f"/gtf/{gtf_path.name}"],
            logger, env={"TMPDIR": "/loom"},
        )
        loom = loom_dir / f"{sample}.loom"
        if not loom.is_file():
            candidates = list(loom_dir.glob("*.loom"))
            if not candidates:
                raise ToolError("velocyto completed without a loom file")
            candidates[0].rename(loom)
        return loom

    def versions(self):
        versions = {"runner": "apptainer"}
        for name, command in {
            "apptainer": [self.executable, "--version"],
            "cellranger": [
                str(self.artifacts["cellranger"] / "cellranger"), "--version"
            ],
        }.items():
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=60
                )
                versions[name] = (
                    result.stdout or result.stderr
                ).strip().splitlines()[0]
            except Exception as exc:
                versions[name] = f"unavailable ({exc})"
        return versions

    def wait_job(self, name):
        return 0

    def job_stats(self):
        return {}

    def job_stats_health(self):
        return {
            "last_ok_at": time.time(), "error": "", "stale": False,
            "jobs": {},
        }

    def resource_info(self):
        return {"memory_total": None, "cpus": os.cpu_count()}
