"""Tool runners: Docker-backed for real runs, mock for dry-run/tests.

Every external tool call goes through one of these, so the pipeline logic is
identical in dry-run and production. Container names are scpre-<sample>-<stage>
so the metrics sampler can attribute per-job CPU/memory via `docker stats`.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .util import run_cmd

CONTAINER_PREFIX = "scpre"


def container_name(sample: str, stage: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in sample)
    return f"{CONTAINER_PREFIX}-{safe}-{stage}"


class ToolError(RuntimeError):
    pass


class DockerUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------
def _find_docker() -> str:
    """Locate the docker CLI even when the service PATH is minimal
    (launchd, cron): check PATH first, then the usual install locations."""
    p = shutil.which("docker")
    if p:
        return p
    for cand in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                 os.path.expanduser("~/.docker/bin/docker")):
        if os.path.exists(cand):
            return cand
    return "docker"


class DockerRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.docker = _find_docker()
        self._job_stats_cache = {}
        self._job_stats_ok_at = None
        self._job_stats_error = ""
        self._job_stats_lock = threading.Lock()
        self._docker_mem_total = None
        self._docker_cpus = None
        self._shutdown = threading.Event()
        self._mount_health_at = 0.0
        self._mount_health_error = ""

    # -- health -----------------------------------------------------------
    def healthy(self) -> bool:
        # generous timeout: Docker Desktop's Resource Saver can take a while
        # to wake the VM on the first probe
        try:
            r = subprocess.run(
                [self.docker, "info", "--format",
                 "{{.ServerVersion}}\t{{.MemTotal}}\t{{.NCPU}}"],
                capture_output=True, text=True, timeout=45)
            if r.returncode == 0:
                parts = r.stdout.strip().split("\t")
                if len(parts) == 3 and parts[1].isdigit():
                    self._docker_mem_total = int(parts[1])
                if len(parts) == 3 and parts[2].isdigit():
                    self._docker_cpus = int(parts[2])
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def resource_info(self) -> dict:
        return {"memory_total": self._docker_mem_total,
                "cpus": self._docker_cpus}

    def mounts_healthy(self, paths) -> str:
        """Return an error when Docker cannot see configured host paths.

        Docker Desktop can remain healthy after an external drive is removed
        and reconnected while its ``/host_mnt`` mapping stays stale.  Cache a
        tiny container probe so the queue can detect that state without
        launching a probe on every five-second tick.
        """
        now = time.time()
        if now - self._mount_health_at < 30:
            return self._mount_health_error
        cmd = [self.docker, "run", "--rm"]
        platform = self.cfg.get("docker", "sratools_platform")
        if platform:
            cmd += ["--platform", platform]
        checks = []
        for i, path in enumerate(paths):
            target = f"/scpre_mount_probe/{i}"
            cmd += ["-v", f"{path}:{target}:ro"]
            checks.append(f"test -e {target}")
        cmd += [self.cfg.get("docker", "sratools_image"),
                "sh", "-lc", " && ".join(checks)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                err = ""
            else:
                detail = (r.stderr or r.stdout).strip().splitlines()
                err = (detail[-1][:300] if detail else
                       f"Docker mount probe exited {r.returncode}")
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
        self._mount_health_at = now
        self._mount_health_error = err
        return err

    def begin_shutdown(self) -> None:
        self._shutdown.set()

    def deferred_cleanup_requires_restart(self) -> bool:
        """Whether host work trees must be released at a Docker-idle point.

        Docker Desktop on macOS uses virtiofs through Apple's Virtualization
        framework.  In practice that VM can retain open handles to bind-mounted
        files after ``docker run --rm`` returns.  Unlinking those files makes
        them invisible without reclaiming their blocks until Docker Desktop is
        restarted.  Keep the workaround macOS-specific and configurable.
        """
        return (sys.platform == "darwin"
                and bool(self.cfg.get(
                    "cleanup", "restart_docker_desktop_at_idle", default=True)))

    def running_containers(self) -> list[str]:
        """Return every running Docker container, managed or otherwise."""
        try:
            r = subprocess.run(
                [self.docker, "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            raise DockerUnavailable(f"cannot list running containers: {e}") from e
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()[:500]
            raise DockerUnavailable(f"cannot list running containers: {msg}")
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def restart_desktop_for_cleanup(self) -> None:
        """Restart Docker Desktop after proving no container is running."""
        if self._shutdown.is_set():
            raise ToolError("service is stopping; cleanup restart cancelled")
        running = self.running_containers()
        if running:
            raise ToolError(
                "refusing to restart Docker Desktop while containers run: "
                + ", ".join(running))
        timeout_s = int(self.cfg.get(
            "cleanup", "docker_desktop_restart_timeout_s", default=180))
        try:
            r = subprocess.run(
                [self.docker, "desktop", "restart", "--timeout", str(timeout_s)],
                capture_output=True, text=True, timeout=timeout_s + 60)
        except Exception as e:  # noqa: BLE001
            raise DockerUnavailable(f"Docker Desktop restart failed: {e}") from e
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()[:500]
            raise DockerUnavailable(f"Docker Desktop restart failed: {msg}")

        # The desktop command may return after the app starts but before the
        # daemon is ready.  Do not let the scheduler launch a replacement job
        # until docker info succeeds.
        self._mount_health_at = 0.0
        self._mount_health_error = ""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._shutdown.is_set():
                raise ToolError("service stopped while Docker Desktop restarted")
            if self.healthy():
                self._mount_health_at = 0.0
                return
            time.sleep(2)
        raise DockerUnavailable(
            f"Docker daemon did not become ready within {timeout_s}s")

    # -- lifecycle --------------------------------------------------------
    def list_jobs(self) -> list[dict]:
        """Return all scpre containers, including stopped containers."""
        try:
            r = subprocess.run(
                [self.docker, "ps", "-a", "--format",
                 "{{.Names}}\t{{.State}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=15)
        except Exception as e:  # noqa: BLE001
            raise DockerUnavailable(f"cannot list Docker jobs: {e}") from e
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()[:500]
            raise DockerUnavailable(f"cannot list Docker jobs: {msg}")
        jobs = []
        for line in r.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[0].startswith(CONTAINER_PREFIX + "-"):
                continue
            jobs.append({"name": parts[0], "state": parts[1],
                         "status": parts[2]})
        return jobs

    def sample_jobs(self, sample: str) -> list[dict]:
        safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_"
                       for ch in sample)
        prefix = f"{CONTAINER_PREFIX}-{safe}-"
        return [j for j in self.list_jobs() if j["name"].startswith(prefix)]

    def assert_sample_idle(self, sample: str) -> None:
        jobs = self.sample_jobs(sample)
        if jobs:
            names = ", ".join(j["name"] for j in jobs)
            raise ToolError(
                f"refusing to modify {sample} while Docker job(s) exist: {names}")

    def _cellranger_work_volume(self, sample: str) -> str:
        prefix = self.cfg.get(
            "docker", "cellranger_work_volume_prefix", default=""
        )
        if not prefix:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sample)
        return f"{prefix}-{safe}"

    def _volume_exists(self, name: str) -> bool:
        if not name:
            return False
        result = subprocess.run(
            [self.docker, "volume", "inspect", name],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0

    def _ensure_volume(self, name: str) -> None:
        if self._volume_exists(name):
            return
        result = subprocess.run(
            [self.docker, "volume", "create", "--label",
             "scpre.managed=true", "--label",
             f"scpre.project={self.cfg.project}", name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise ToolError(f"could not create Docker volume {name}: {detail}")

    def _remove_volume(self, name: str) -> None:
        if not self._volume_exists(name):
            return
        result = subprocess.run(
            [self.docker, "volume", "rm", name],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise ToolError(f"could not remove Docker volume {name}: {detail}")

    def clean_partial(self, sample: str, stage: str) -> None:
        """Remove Linux-backed partial state only for an explicit retry."""
        if stage == "cellranger":
            self._remove_volume(self._cellranger_work_volume(sample))

    def cleanup_sample_storage(self, sample: str) -> None:
        """Reclaim the pipestance volume after archive verification."""
        self.assert_sample_idle(sample)
        self._remove_volume(self._cellranger_work_volume(sample))

    def _volume_free_bytes(self, volume: str, image: str,
                           platform: str) -> int:
        cmd = [self.docker, "run", "--rm"]
        if platform:
            cmd += ["--platform", platform]
        cmd += ["-v", f"{volume}:/work", image,
                "df", "-B1", "--output=avail", "/work"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise ToolError(
                f"could not measure Docker volume {volume}: {detail}"
            )
        values = [line.strip() for line in result.stdout.splitlines()
                  if line.strip().isdigit()]
        if not values:
            raise ToolError(
                f"Docker volume {volume} returned no free-space value"
            )
        return int(values[-1])

    def _volume_file_exists(self, volume: str, path: str, image: str,
                            platform: str) -> bool:
        cmd = [self.docker, "run", "--rm"]
        if platform:
            cmd += ["--platform", platform]
        cmd += ["-v", f"{volume}:/work:ro", image, "test", "-f", path]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        detail = (result.stderr or result.stdout).strip()[:500]
        raise ToolError(f"could not inspect Docker volume {volume}: {detail}")

    def _export_volume_tree(self, volume: str, source: str,
                            destination_parent: Path, image: str,
                            platform: str, logger) -> Path:
        """Copy a volume tree to Windows, following Linux symlinks."""
        destination_parent.mkdir(parents=True, exist_ok=True)
        destination = destination_parent / Path(source).name
        if destination.exists():
            shutil.rmtree(destination)
        # ``docker cp -L`` follows only the top-level source link. Nested
        # symlinks still reach the Windows daemon, which cannot create them
        # without a special host privilege. GNU cp runs inside Linux, follows
        # every link there, and writes ordinary files through the bind mount.
        sample = Path(source).parent.name
        self._run(
            image, platform,
            [(volume, "/work", True), (destination_parent, "/export", False)],
            ["cp", "-RL", "--preserve=timestamps", source, "/export/"],
            container_name(sample, "export-outs"), logger,
        )
        return destination

    def wait_job(self, name: str) -> int:
        """Wait for an already-running container and return its exit code.

        ``docker wait`` attaches to the daemon rather than the process that
        originally issued ``docker run``.  This lets a restarted scheduler
        safely adopt a Cell Ranger container that survived a dashboard/service
        crash.
        """
        try:
            r = subprocess.run([self.docker, "wait", name],
                               capture_output=True, text=True)
        except Exception as e:  # noqa: BLE001
            raise DockerUnavailable(f"cannot wait for Docker job {name}: {e}") from e
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()[:500]
            raise DockerUnavailable(f"cannot wait for Docker job {name}: {msg}")
        try:
            return int(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise DockerUnavailable(
                f"Docker job {name} returned an invalid exit status: {r.stdout!r}") from e

    def stop_sample_jobs(self, sample: str, logger=None,
                         timeout_s: int = 20) -> list[str]:
        """Stop and remove containers for one exact sample; return errors."""
        errors = []
        for job in self.sample_jobs(sample):
            name = job["name"]
            if logger:
                logger.warning("stopping recovered Docker job %s", name)
            if job["state"] == "running":
                r = subprocess.run(
                    [self.docker, "stop", "--time", str(timeout_s), name],
                    capture_output=True, text=True, timeout=timeout_s + 30)
                if r.returncode != 0 and "No such container" not in (r.stderr or ""):
                    errors.append(f"stop {name}: {(r.stderr or r.stdout).strip()[:300]}")
                    continue
            # Old jobs use --rm and may already be gone; future-proof recovery
            # by also removing any stopped container left by a changed runner.
            r = subprocess.run([self.docker, "rm", "-f", name],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0 and "No such container" not in (r.stderr or ""):
                errors.append(f"remove {name}: {(r.stderr or r.stdout).strip()[:300]}")
        return errors

    def _run(self, image, platform, mounts, argv, name, logger,
             workdir=None, env=None, cpus=None):
        if self._shutdown.is_set():
            raise ToolError("service is stopping; refusing to start a new Docker job")
        existing = [j for j in self.list_jobs() if j["name"] == name]
        if existing:
            raise ToolError(
                f"refusing to start duplicate Docker job {name}; "
                f"existing container is {existing[0]['status']}")
        cmd = [self.docker, "run", "--rm", "--name", name, "--init",
               "--label", "scpre.managed=true",
               "--label", f"scpre.project={self.cfg.project}"]
        if platform:
            cmd += ["--platform", platform]
        for host, cont, ro in mounts:
            cmd += ["-v", f"{host}:{cont}:{'ro' if ro else 'rw'}"]
        if workdir:
            cmd += ["-w", workdir]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        if cpus:
            cmd += ["--cpus", str(cpus)]
        cmd += [image] + [str(a) for a in argv]
        if self._shutdown.is_set():
            raise ToolError("service is stopping; refusing to start a new Docker job")
        res = run_cmd(cmd, logger=logger)
        if res.returncode != 0:
            tail = "\n".join(res.stdout.splitlines()[-15:])
            raise ToolError(f"{argv[0]} failed (exit {res.returncode}):\n{tail}")
        return res

    # -- tools ------------------------------------------------------------
    def prefetch(self, srr: str, dest_dir: Path, sample: str, logger):
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._run(
                self.cfg.get("docker", "sratools_image"),
                self.cfg.get("docker", "sratools_platform"),
                [(dest_dir, "/out", False)],
                ["prefetch", "--max-size", "u", "-O", "/out", srr],
                container_name(sample, f"prefetch-{srr}"), logger)
        except ToolError as exc:
            if (os.name != "nt" or not re.fullmatch(r"[SED]RR\d+", srr)
                    or not any(marker in str(exc).lower() for marker in
                               ("certificate", "mbedtls", "encryption failed"))):
                raise
            logger.warning("prefetch TLS failed; using verified HTTPS public SRA mirror")
            return self._prefetch_public_s3(srr, dest_dir, sample, logger)
        for cand in (dest_dir / srr / f"{srr}.sra", dest_dir / f"{srr}.sra"):
            if cand.exists():
                return cand
        raise ToolError(f"prefetch finished but {srr}.sra not found under {dest_dir}")

    def _prefetch_public_s3(self, srr: str, dest_dir: Path, sample: str, logger):
        """Windows HTTPS fallback; publish only a size-checked, validated SRA."""
        if not re.fullmatch(r"[SED]RR\d+", srr):
            raise ToolError("invalid run accession for public SRA download")
        curl = shutil.which("curl.exe")
        if not curl:
            raise ToolError("Windows curl.exe is required for the secure SRA fallback")
        url = f"https://sra-pub-run-odp.s3.amazonaws.com/sra/{srr}/{srr}"
        curl_base = [curl]
        proxy = self.cfg.get("docker", "download_proxy", default="")
        if proxy:
            curl_base += ["--proxy", str(proxy)]
        # curl's default --retry does not cover a transient TLS handshake
        # failure (35). Retry only transport failures; certificate failures
        # (60) remain terminal and never disable verification.
        transport_errors = {5, 6, 7, 18, 28, 35, 52, 55, 56}
        for attempt in range(4):
            head = subprocess.run(
                curl_base + ["--fail", "--silent", "--show-error", "--head",
                 "--proto", "=https", "--connect-timeout", "20", "--max-time", "60", url],
                capture_output=True, text=True, timeout=65,
            )
            if head.returncode not in transport_errors or attempt == 3:
                break
            logger.warning("SRA mirror HEAD transport error %d; retry %d/3",
                           head.returncode, attempt + 1)
            if self._shutdown.wait(2 ** attempt):
                raise ToolError("service stopped during SRA HEAD retry")
        sizes = re.findall(r"^Content-Length:\s*(\d+)\s*$", head.stdout,
                           re.IGNORECASE | re.MULTILINE)
        if head.returncode or not sizes or int(sizes[-1]) <= 0:
            raise ToolError(f"public SRA mirror HEAD failed: {head.stderr[:500]}")
        expected = int(sizes[-1])
        dest_dir.mkdir(parents=True, exist_ok=True)
        part = dest_dir / f"{srr}.sra.part"
        target = dest_dir / f"{srr}.sra"
        if target.exists():
            raise ToolError(f"refusing to overwrite existing SRA: {target}")
        if self._shutdown.is_set():
            raise ToolError("service is stopping; refusing public SRA download")
        logger.info("HTTPS SRA mirror: %s; expected bytes=%d", url, expected)
        if not part.exists() or part.stat().st_size != expected:
            cmd = curl_base + ["--fail", "--silent", "--show-error", "--proto", "=https",
                   "--connect-timeout", "20", "--max-time", "86400",
                   "--continue-at", "-", "--output", str(part), url]
            for attempt in range(4):
                # Keep retries here rather than inside curl: every timeout
                # must recalculate -C - from the surviving partial, including
                # very large runs that need more than one 24-hour attempt.
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, errors="replace")
                while True:
                    try:
                        output, _ = proc.communicate(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        if self._shutdown.is_set():
                            proc.terminate()
                            try:
                                proc.communicate(timeout=10)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.communicate()
                            raise ToolError("service stopped; partial HTTPS SRA download preserved")
                if proc.returncode == 0:
                    break
                if proc.returncode not in transport_errors or attempt == 3:
                    raise ToolError(f"public SRA HTTPS download failed: {output[-1000:]}")
                logger.warning("SRA HTTPS transport error %d; resume retry %d/3",
                               proc.returncode, attempt + 1)
                if self._shutdown.wait(2 ** attempt):
                    raise ToolError("service stopped; partial HTTPS SRA download preserved")
        if part.stat().st_size != expected:
            raise ToolError(f"public SRA size mismatch for {srr}")
        with part.open("rb") as fh:
            if fh.read(8) != b"NCBI.sra":
                raise ToolError(f"public SRA mirror returned an invalid archive for {srr}")
        self._run(
            self.cfg.get("docker", "sratools_image"),
            self.cfg.get("docker", "sratools_platform"),
            [(dest_dir, "/in", True)], ["vdb-validate", f"/in/{part.name}"],
            container_name(sample, f"validate-download-{srr}"), logger,
        )
        part.replace(target)
        logger.info("public HTTPS SRA verified: %s (%d bytes)", target, expected)
        return target

    def fasterq(self, sra_path: Path, out_dir: Path, tmp_dir: Path,
                threads: int, sample: str, logger):
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        res = self._run(
            self.cfg.get("docker", "sratools_image"),
            self.cfg.get("docker", "sratools_platform"),
            [(sra_path.parent, "/in", True), (out_dir, "/out", False),
             (tmp_dir, "/tmp_fq", False)],
            ["fasterq-dump", "--split-files", "--include-technical",
             "-e", threads, "-t", "/tmp_fq", "-O", "/out",
             f"/in/{sra_path.name}"],
            container_name(sample, f"fasterq-{sra_path.stem}"), logger)
        produced = sorted(out_dir.glob(f"{sra_path.stem}_*.fastq"))
        if not produced:
            produced = sorted(out_dir.glob(f"{sra_path.stem}*.fastq"))
        if not produced:
            raise ToolError(f"fasterq-dump produced no fastq for {sra_path.name}")
        spots = None
        for line in res.stdout.splitlines():
            if "spots read" in line:
                digits = "".join(ch for ch in line.split(":")[-1] if ch.isdigit())
                if digits:
                    spots = int(digits)
        return produced, spots

    def compress(self, paths, threads: int, sample: str, logger):
        """Compress inside the sratools container (keeps host deps at zero)."""
        out = []
        compressor = self.cfg.get(
            "docker", "compressor", default="pigz"
        ).lower()
        for p in paths:
            if compressor == "gzip":
                argv = ["gzip", "-f", f"/d/{p.name}"]
            else:
                argv = ["pigz", "-f", "-p", threads, f"/d/{p.name}"]
            self._run(
                self.cfg.get("docker", "sratools_image"),
                self.cfg.get("docker", "sratools_platform"),
                [(p.parent, "/d", False)],
                argv,
                container_name(sample, "pigz"), logger)
            out.append(p.with_name(p.name + ".gz"))
        return out

    def cellranger_count(self, sample: str, fastq_dir: Path, run_dir: Path,
                         ref_dir: Path, cores: int, mem_gb: int,
                         create_bam: bool, logger):
        run_dir.mkdir(parents=True, exist_ok=True)
        cr_dir = self.cfg.path("cellranger_dir")
        cr_source = self.cfg.get(
            "docker", "cellranger_volume", default=""
        ) or cr_dir
        work_volume = self._cellranger_work_volume(sample)
        work_source = work_volume or run_dir
        image = self.cfg.get("docker", "cellranger_image")
        platform = self.cfg.get("docker", "cellranger_platform")
        pipestance_complete = False
        if work_volume:
            self._ensure_volume(work_volume)
            fastq_bytes = sum(
                path.stat().st_size for path in fastq_dir.glob("*.fastq.gz")
                if path.is_file()
            )
            multiplier = float(self.cfg.get(
                "space", "cellranger_multiplier", default=2.5
            ))
            reserve = float(self.cfg.get(
                "docker", "cellranger_volume_min_free_gb", default=100
            )) * 1024 ** 3
            required = int(fastq_bytes * multiplier) + int(reserve)
            available = self._volume_free_bytes(
                work_volume, image, platform
            )
            if available < required:
                raise ToolError(
                    f"Docker ext4 storage has {available / 1024 ** 3:.1f} GiB "
                    f"free; Cell Ranger for {sample} requires about "
                    f"{required / 1024 ** 3:.1f} GiB including reserve"
                )
            logger.info(
                "Cell Ranger pipestance uses Docker ext4 volume %s "
                "(%.1f GiB free)",
                work_volume, available / 1024 ** 3,
            )
            pipestance_complete = self._volume_file_exists(
                work_volume, f"/work/{sample}/_finalstate", image, platform,
            )
            self._run(
                image, platform,
                [(work_volume, "/work", False)],
                ["sh", "-c",
                 "mkdir -p /work/tmp; "
                 "if [ ! -f \"$1/_finalstate\" ]; then "
                 "rm -f \"$1/_lock\"; fi",
                 "sh", f"/work/{sample}"],
                container_name(sample, "volume-init"), logger,
            )
        else:
            tmp = run_dir / "tmp"
            tmp.mkdir(exist_ok=True)
        if pipestance_complete:
            logger.info(
                "completed Cell Ranger pipestance found in %s; exporting outs",
                work_volume,
            )
        else:
            override_mounts = []
            override_args = []
            override_setting = self.cfg.get("docker", "cellranger_overrides", default="")
            if override_setting:
                override_path = Path(override_setting)
                if not override_path.is_absolute():
                    override_path = self.cfg.root / override_path
                if not override_path.is_file():
                    raise ToolError(f"Cell Ranger overrides file missing: {override_path}")
                override_mounts = [(override_path, "/resource-overrides.json", True)]
                override_args = ["--overrides=/resource-overrides.json"]
                logger.info("using Cell Ranger resource overrides: %s", override_path)
            self._run(
                image,
                platform,
                [(cr_source, "/opt/cellranger", True), (ref_dir, "/ref", True),
                 (fastq_dir, "/fastq", True),
                 (work_source, "/work", False)] + override_mounts,
                ["/opt/cellranger/cellranger", "count",
                 f"--id={sample}", "--fastqs=/fastq", f"--sample={sample}",
                 "--transcriptome=/ref",
                 f"--localcores={cores}", f"--localmem={mem_gb}",
                 f"--create-bam={'true' if create_bam else 'false'}",
                 "--disable-ui"] + override_args,
                container_name(sample, "cellranger"), logger,
                workdir="/work", env={"TMPDIR": "/work/tmp"})
        if work_volume:
            self._export_volume_tree(
                work_volume, f"/work/{sample}/outs", run_dir / sample,
                image, platform, logger,
            )
        outs = run_dir / sample / "outs"
        if not outs.is_dir():
            raise ToolError(f"cellranger finished but {outs} is missing")
        return outs

    def velocyto(self, sample: str, outs_dir: Path, gtf_path: Path,
                 loom_dir: Path, threads: int, logger):
        loom_dir.mkdir(parents=True, exist_ok=True)
        bam = outs_dir / "possorted_genome_bam.bam"
        barcodes = outs_dir / "filtered_feature_bc_matrix" / "barcodes.tsv.gz"
        if not bam.exists():
            raise ToolError(f"BAM missing for velocyto: {bam}")
        if not barcodes.exists():
            raise ToolError(f"barcodes missing for velocyto: {barcodes}")
        # velocyto launches ``samtools sort`` beside the input BAM and names
        # the temporary output ``cellsorted_possorted_genome_bam.bam``.  The
        # mount therefore has to be writable even though the validated Cell
        # Ranger files themselves are inputs.  Remove only velocyto's scratch
        # BAM afterwards so it cannot inflate the durable Cell Ranger archive.
        scratch_glob = "cellsorted_possorted_genome_bam.bam*"
        image = self.cfg.get("docker", "velocyto_image")
        platform = self.cfg.get("docker", "velocyto_platform")
        work_volume = self._cellranger_work_volume(sample)
        if work_volume:
            if not self._volume_exists(work_volume):
                raise ToolError(
                    f"Cell Ranger work volume is missing for {sample}: "
                    f"{work_volume}"
                )
            outs_source = work_volume
            outs_target = f"/cellranger/{sample}/outs"
            outs_mount = "/cellranger"
        else:
            outs_source = outs_dir
            outs_target = "/outs"
            outs_mount = "/outs"
        try:
            self._run(
                image, platform,
                [(outs_source, outs_mount, False),
                 (gtf_path.parent, "/gtf", True),
                 (loom_dir, "/loom", False)],
                ["velocyto", "run",
                 "-b", f"{outs_target}/filtered_feature_bc_matrix/barcodes.tsv.gz",
                 "-o", "/loom", "-e", sample, "-@", threads,
                 f"{outs_target}/possorted_genome_bam.bam",
                 f"/gtf/{gtf_path.name}"],
                container_name(sample, "velocyto"), logger,
                env={"TMPDIR": "/loom"})
        finally:
            if work_volume:
                try:
                    self._run(
                        image, platform,
                        [(work_volume, "/cellranger", False)],
                        ["sh", "-c",
                         "find \"$1\" -maxdepth 1 -type f "
                         "-name 'cellsorted_possorted_genome_bam.bam*' -delete",
                         "sh", outs_target],
                        container_name(sample, "velocyto-cleanup"), logger,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "could not remove velocyto scratch from %s: %s",
                        work_volume, e,
                    )
            else:
                for scratch in outs_dir.glob(scratch_glob):
                    try:
                        scratch.unlink()
                    except OSError as e:
                        logger.warning(
                            "could not remove velocyto scratch %s: %s",
                            scratch, e,
                        )
        loom = loom_dir / f"{sample}.loom"
        if not loom.exists():
            looms = list(loom_dir.glob("*.loom"))
            if looms:
                looms[0].rename(loom)
            else:
                raise ToolError(f"velocyto finished but no .loom in {loom_dir}")
        return loom

    def versions(self) -> dict:
        v = {"runner": "docker"}
        cr_source = self.cfg.get(
            "docker", "cellranger_volume", default=""
        ) or self.cfg.path("cellranger_dir")
        probes = {
            "docker": [self.docker, "--version"],
            "cellranger": [self.docker, "run", "--rm",
                           "--platform", self.cfg.get("docker", "cellranger_platform"),
                           "-v", f"{cr_source}:/opt/cellranger:ro",
                           self.cfg.get("docker", "cellranger_image"),
                           "/opt/cellranger/cellranger", "--version"],
            "sra-tools": [self.docker, "run", "--rm",
                          "--platform", self.cfg.get("docker", "sratools_platform"),
                          self.cfg.get("docker", "sratools_image"),
                          "fasterq-dump", "--version"],
            "velocyto": [self.docker, "run", "--rm",
                         self.cfg.get("docker", "velocyto_image"),
                         "velocyto", "--version"],
        }
        for name, cmd in probes.items():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                v[name] = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "?"
            except Exception as e:  # noqa: BLE001
                v[name] = f"unavailable ({e})"
        return v

    def job_stats(self) -> dict:
        """Per-container CPU/mem for scpre-* containers via docker stats."""
        try:
            r = subprocess.run(
                [self.docker, "stats", "--no-stream", "--format",
                 "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
                capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                raise DockerUnavailable((r.stderr or r.stdout).strip()[:500])
            jobs = {}
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 3 and parts[0].startswith(CONTAINER_PREFIX + "-"):
                    jobs[parts[0]] = {"cpu": parts[1], "mem": parts[2]}
            with self._job_stats_lock:
                self._job_stats_cache = jobs
                self._job_stats_ok_at = time.time()
                self._job_stats_error = ""
            return jobs
        except Exception as e:  # noqa: BLE001
            # A transient Docker Desktop timeout must not become the false
            # dashboard claim "no containers running". Keep the last known
            # values and expose their staleness separately.
            with self._job_stats_lock:
                self._job_stats_error = str(e)
                return dict(self._job_stats_cache)

    def job_stats_health(self) -> dict:
        with self._job_stats_lock:
            return {"last_ok_at": self._job_stats_ok_at,
                    "error": self._job_stats_error,
                    "stale": bool(self._job_stats_error),
                    "jobs": dict(self._job_stats_cache)}


# --------------------------------------------------------------------------
class MockRunner:
    """Fixture-backed runner for dry-run and tests. No network, no Docker.

    Produces small but structurally valid outputs so every validation step in
    the real pipeline (naming, gzip magic, HDF5 magic, matrices, checksums)
    exercises the same code paths.
    """

    def __init__(self, cfg, fail_stage: str = None):
        self.cfg = cfg
        self.fail_stage = fail_stage  # for testing retry paths
        self.live_jobs = {}
        self.defer_cleanup = False
        self.external_containers = []
        self.desktop_restarts = 0

    def healthy(self) -> bool:
        return True

    def mounts_healthy(self, paths) -> str:
        return ""

    def begin_shutdown(self):
        pass

    def deferred_cleanup_requires_restart(self):
        return self.defer_cleanup

    def running_containers(self):
        managed = [j["name"] for j in self.live_jobs.values()
                   if j.get("state") == "running"]
        return managed + list(self.external_containers)

    def restart_desktop_for_cleanup(self):
        running = self.running_containers()
        if running:
            raise ToolError("containers still running: " + ", ".join(running))
        self.desktop_restarts += 1

    def list_jobs(self):
        return [dict(v) for v in self.live_jobs.values()]

    def sample_jobs(self, sample):
        prefix = f"{CONTAINER_PREFIX}-{sample}-"
        return [j for j in self.list_jobs() if j["name"].startswith(prefix)]

    def assert_sample_idle(self, sample):
        jobs = self.sample_jobs(sample)
        if jobs:
            raise ToolError(f"refusing to modify {sample} while Docker jobs exist")

    def stop_sample_jobs(self, sample, logger=None, timeout_s=20):
        for j in self.sample_jobs(sample):
            self.live_jobs.pop(j["name"], None)
        return []

    def clean_partial(self, sample, stage):
        pass

    def cleanup_sample_storage(self, sample):
        pass

    @staticmethod
    def _write_fastq(path: Path, n_reads: int, read_len: int, seed: int):
        rng = random.Random(seed)
        with open(path, "w") as f:
            for i in range(n_reads):
                seq = "".join(rng.choice("ACGT") for _ in range(read_len))
                f.write(f"@MOCK.{i} {i}/1\n{seq}\n+\n{'F' * read_len}\n")

    def prefetch(self, srr, dest_dir: Path, sample, logger):
        if self.fail_stage == "downloading":
            raise ToolError("mock prefetch failure (injected)")
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / f"{srr}.sra"
        p.write_bytes(b"NCBI.sra-mock" + os.urandom(256))
        logger.info("mock prefetch -> %s", p)
        return p

    def fasterq(self, sra_path: Path, out_dir: Path, tmp_dir, threads,
                sample, logger):
        if self.fail_stage == "converting":
            raise ToolError("mock fasterq failure (injected)")
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = sra_path.stem
        seed = hash(stem) & 0xFFFF
        f1 = out_dir / f"{stem}_1.fastq"   # 28 bp -> R1 (CB+UMI)
        f2 = out_dir / f"{stem}_2.fastq"   # 90 bp -> R2 (cDNA)
        self._write_fastq(f1, 200, 28, seed)
        self._write_fastq(f2, 200, 90, seed + 1)
        logger.info("mock fasterq-dump -> %s, %s", f1.name, f2.name)
        return [f1, f2], 200

    def compress(self, paths, threads, sample, logger):
        out = []
        for p in paths:
            gz = p.with_name(p.name + ".gz")
            with open(p, "rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
            out.append(gz)
        return out

    def cellranger_count(self, sample, fastq_dir: Path, run_dir: Path,
                         ref_dir, cores, mem_gb, create_bam, logger):
        if self.fail_stage == "cellranger":
            raise ToolError("mock cellranger failure (injected)")
        fq = sorted(fastq_dir.glob(f"{sample}_S1_L*_R1_001.fastq.gz"))
        if not fq:
            raise ToolError(f"mock cellranger: no correctly named FASTQs for "
                            f"{sample} in {fastq_dir}")
        outs = run_dir / sample / "outs"
        mat = outs / "filtered_feature_bc_matrix"
        mat.mkdir(parents=True, exist_ok=True)
        (outs / "web_summary.html").write_text(
            f"<html><body><h1>Mock web_summary for {sample}</h1></body></html>")
        (outs / "metrics_summary.csv").write_text(
            "Estimated Number of Cells,Mean Reads per Cell\n1000,20000\n")
        for name in ("barcodes.tsv.gz", "features.tsv.gz", "matrix.mtx.gz"):
            with gzip.open(mat / name, "wb") as f:
                f.write(b"mock-tsv-content\n" * 32)
        if create_bam:
            (outs / "possorted_genome_bam.bam").write_bytes(
                b"BAM\x01" + os.urandom(1024))
        logger.info("mock cellranger count -> %s", outs)
        return outs

    def velocyto(self, sample, outs_dir: Path, gtf_path, loom_dir: Path,
                 threads, logger):
        if self.fail_stage == "loom":
            raise ToolError("mock velocyto failure (injected)")
        loom_dir.mkdir(parents=True, exist_ok=True)
        loom = loom_dir / f"{sample}.loom"
        loom.write_bytes(b"\x89HDF\r\n\x1a\n" + os.urandom(2048))  # HDF5 magic
        logger.info("mock velocyto -> %s", loom)
        return loom

    def versions(self):
        return {"runner": "mock", "cellranger": "cellranger mock-10.1.0",
                "sra-tools": "fasterq-dump mock-3.1", "velocyto": "mock-0.17"}

    def wait_job(self, name: str) -> int:
        while name in self.live_jobs:
            time.sleep(0.05)
        return 0

    def job_stats(self):
        return {}

    def job_stats_health(self):
        return {"last_ok_at": time.time(), "error": "", "stale": False,
                "jobs": {}}

    def resource_info(self):
        return {"memory_total": None, "cpus": None}


def make_runner(cfg, **kw):
    if cfg.get("tools", "mode") == "mock":
        return MockRunner(cfg, **kw)
    return DockerRunner(cfg)
