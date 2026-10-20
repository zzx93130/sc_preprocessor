"""Cluster manager: intake, immutable manifests, PBS dispatch, recovery."""
from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path

from . import sra_table
from .artifact_store import load_catalog
from .cluster_manifest import write_manifest
from .cluster_manifest import SAFE_ID
from .executors.pbs import PBSExecutor
from .incoming import IncomingStore
from .scratch import heartbeat_age, read_heartbeat, verify_mount
from .space import estimate
from .util import app_logger, disk_usage, sample_logger

log = app_logger()
TERMINAL_ATTEMPTS = {"completed", "failed", "interrupted", "cancelled"}


class ClusterManager:
    def __init__(self, cfg, state, executor=None):
        self.cfg = cfg
        self.state = state
        cluster = cfg.data["cluster"]
        self.cluster = cluster
        self.shared_root = Path(cluster["shared_root"])
        self.incoming = IncomingStore(
            self.shared_root,
            prefetch=str(cluster.get("prefetch", "prefetch")),
            validator=str(cluster.get("vdb_validate", "vdb-validate")),
        )
        self.executor = executor or PBSExecutor(cluster["pbs"])
        self._stop = False
        self._last_stats_error = ""
        self._last_stats_ok_at = 0.0

    @property
    def workers(self):
        return {
            attempt["sample_id"]: attempt["attempt_id"]
            for attempt in self.state.list_active_attempts()
        }

    def paused_reason(self):
        return self.state.kv_get("paused_reason")

    def pause(self, reason: str):
        if self.paused_reason() != reason:
            self.state.kv_set("paused_reason", reason)
            log.warning("QUEUE PAUSED: %s", reason)

    def resume(self):
        self.state.kv_set("paused_reason", None)
        log.info("queue resumed")

    def cleanup_state(self):
        pending = [
            attempt["sample_id"] for attempt in self.state.list_active_attempts()
            if attempt["status"] == "archiving"
        ]
        return {
            "pending_samples": pending,
            "status": (
                "Worker result retained until the Gluster archive is verified"
                if pending else None
            ),
        }

    def _shared_mount(self) -> Path:
        configured = self.cluster.get("shared_mount", "")
        return Path(configured) if configured else self.shared_root.parent

    def _environment_error(self) -> str:
        try:
            verify_mount(
                self._shared_mount(),
                self.cluster["shared_identity"],
                require_mountpoint=bool(
                    self.cluster.get("require_mountpoints", True)
                ),
            )
        except Exception as exc:
            return str(exc)
        if not self.executor.healthy():
            return "PBS commands are unavailable"
        # The state directory may not exist yet on a first deployment.  Create
        # manager-owned paths before measuring them so the free-space probe can
        # resolve the containing filesystem instead of reporting a false error.
        try:
            self.cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (
                "required manager path is not writable: "
                f"{self.cfg.state_db.parent} ({exc})"
            )
        manager_disk = disk_usage(self.cfg.state_db.parent)
        manager_min = float(
            self.cluster.get("manager_min_free_gb", 20)
        ) * 1024 ** 3
        if (not manager_disk.get("ok")
                or manager_disk.get("free", 0) < manager_min):
            return (
                f"manager state/log disk has "
                f"{manager_disk.get('free', 0) / 1024 ** 3:.1f} GiB free; "
                f"{manager_min / 1024 ** 3:.1f} GiB is required"
            )
        for path in (
            self.cfg.state_db.parent, self.cfg.logs_dir,
            self.shared_root / "control",
            self.shared_root / "archive",
            self.incoming.ready,
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".scpre-manager-write-test"
                probe.write_text("ok")
                probe.unlink()
            except OSError as exc:
                return f"required manager path is not writable: {path} ({exc})"
        return ""

    def scan_inbox(self) -> int:
        table = sra_table.discover(
            self.cfg.root, self.incoming.ready,
            self.cfg.get("metadata", "sra_run_table", default=""),
        )
        if not table:
            return 0
        try:
            groups = sra_table.parse(table)
        except ValueError as exc:
            log.warning("SraRunTable parse problem: %s", exc)
            return 0
        process_all = bool(
            self.cfg.get("metadata", "process_all", default=False)
        )
        download_enabled = bool(self.cluster.get("download_enabled", True))
        budget = int(self.cluster.get("download_ahead", 2))
        created = 0
        for sample_id, group in groups.items():
            if self.state.get_sample(sample_id):
                continue
            records = {}
            for srr in group["srrs"]:
                record = self.incoming.record_for(srr)
                if (not record and process_all and download_enabled
                        and budget > 0 and not self.paused_reason()):
                    try:
                        record = self.incoming.download(srr)
                        budget -= 1
                    except Exception as exc:
                        log.warning("download %s failed: %s", srr, exc)
                if record:
                    records[srr] = record
            if len(records) != len(group["srrs"]):
                continue
            meta = dict(group["meta"])
            meta["input_paths"] = [
                records[srr]["path"] for srr in group["srrs"]
            ]
            meta["input_records"] = records
            meta["sra_run_table"] = str(table)
            self.state.upsert_sample(
                sample_id, project=self.cfg.project, mode="sra",
                srrs=group["srrs"], status="queued", stage="ready",
                meta=meta,
            )
            created += 1
        return created

    def add_fastq_sample(self, sample_id: str, fastq_paths) -> None:
        if not SAFE_ID.fullmatch(sample_id):
            raise ValueError(
                "sample id may contain only letters, numbers, dot, dash, underscore"
            )
        if self.state.get_sample(sample_id):
            raise ValueError(f"sample already exists: {sample_id}")
        records = [
            self.incoming.ingest_fastq(sample_id, Path(path))
            for path in fastq_paths
        ]
        if not records:
            raise ValueError("no FASTQ inputs were provided")
        self.state.upsert_sample(
            sample_id, project=self.cfg.project, mode="fastq", srrs=[],
            status="queued", stage="ready",
            meta={
                "input_paths": [record["path"] for record in records],
                "input_records": records,
            },
        )

    def _heartbeat(self, attempt_id: str) -> dict | None:
        return read_heartbeat(
            self.shared_root / "control" / "heartbeats"
            / f"{attempt_id}.json"
        )

    def _event(self, attempt_id: str) -> dict | None:
        path = (
            self.shared_root / "control" / "events" / f"{attempt_id}.json"
        )
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _committed_record(self, sample_id: str) -> dict | None:
        path = self.cfg.archive_sample_dir(sample_id) / "COMMITTED.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _complete(self, attempt: dict, event: dict | None,
                  committed: dict) -> None:
        attempt_id = attempt["attempt_id"]
        if committed.get("attempt_id") != attempt_id:
            return
        if committed.get("manifest_sha256") != attempt["manifest_sha256"]:
            self._fail(
                attempt, "archive manifest checksum does not match the attempt"
            )
            return
        event_id = (self.state.get_sample(attempt["sample_id"]) or {}).get(
            "meta", {}
        ).get("pbs_event_id")
        if event_id:
            self.state.event_end(int(event_id), "completed", 0)
        node = (event or {}).get("node") or attempt.get("node") or ""
        archive_path = str(self.cfg.archive_sample_dir(attempt["sample_id"]))
        self.state.update_attempt(
            attempt_id, status="completed", pbs_state="finished",
            node=node, exit_code=0, ended_at=time.time(), error="",
        )
        self.state.update_meta(
            attempt["sample_id"], attempt_id=attempt_id,
            pbs_job_id=attempt.get("pbs_job_id", ""), compute_node=node,
            pbs_state="finished", pbs_exit_code=0,
            archive_dir=archive_path, archive_pending=False,
        )
        self.state.upsert_sample(
            attempt["sample_id"], status="done", stage="done", error=""
        )
        try:
            self._backup_state(attempt_id)
            self.state.kv_set("cluster_backup_warning", None)
        except Exception as exc:
            self.state.kv_set(
                "cluster_backup_warning",
                f"state backup failed after {attempt['sample_id']}: {exc}",
            )
            log.error("state backup failed: %s", exc)
        if self.cluster.get("remove_incoming_after_commit", False):
            sample = self.state.get_sample(attempt["sample_id"])
            self.incoming.remove_committed(sample["srrs"])
        log.info("%s completed on %s; archive committed",
                 attempt["sample_id"], node or "worker")

    def _backup_state(self, label: str = "periodic") -> Path:
        root = self.shared_root / "control" / "state-backups"
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = root / f"state-{stamp}-{label}.sqlite"
        self.state.backup_to(target)
        keep = max(2, int(self.cluster.get("state_backup_keep", 30)))
        backups = sorted(root.glob("state-*.sqlite"), reverse=True)
        for old in backups[keep:]:
            old.unlink(missing_ok=True)
        return target

    def _fail(self, attempt: dict, message: str, event=None) -> None:
        attempt_id = attempt["attempt_id"]
        event = event or {}
        exit_code = event.get("exit_code")
        node = event.get("node") or attempt.get("node") or ""
        scratch_path = event.get("scratch_path") or attempt.get(
            "scratch_path", ""
        )
        event_id = (self.state.get_sample(attempt["sample_id"]) or {}).get(
            "meta", {}
        ).get("pbs_event_id")
        if event_id:
            self.state.event_end(
                int(event_id), "failed",
                int(exit_code) if exit_code is not None else 1, message,
            )
        self.state.update_attempt(
            attempt_id, status="failed", node=node,
            scratch_path=scratch_path, exit_code=exit_code,
            error=message[:4000], ended_at=time.time(),
        )
        self.state.update_meta(
            attempt["sample_id"], attempt_id=attempt_id,
            pbs_job_id=attempt.get("pbs_job_id", ""), compute_node=node,
            pbs_state="failed", pbs_exit_code=exit_code,
            retained_scratch=scratch_path, retry_affinity=node,
            archive_pending=False,
        )
        self.state.upsert_sample(
            attempt["sample_id"], status="failed",
            stage=event.get("stage") or "pbs", error=message[:1000],
        )

    def reconcile(self) -> None:
        try:
            verify_mount(
                self._shared_mount(), self.cluster["shared_identity"],
                require_mountpoint=bool(
                    self.cluster.get("require_mountpoints", True)
                ),
            )
        except Exception as exc:
            if self.paused_reason() != "paused from dashboard":
                self.pause(
                    f"shared filesystem unavailable during reconciliation: {exc}"
                )
            return
        for attempt in self.state.list_active_attempts():
            attempt_id = attempt["attempt_id"]
            event = self._event(attempt_id)
            committed = self._committed_record(attempt["sample_id"])
            if committed:
                self._complete(attempt, event, committed)
                continue
            if event and event.get("manifest_sha256") != attempt[
                    "manifest_sha256"]:
                self._fail(attempt, "worker event checksum identity mismatch")
                continue
            if event and event.get("status") == "failed":
                self._fail(
                    attempt, event.get("error", "worker failed"), event
                )
                continue
            heartbeat = self._heartbeat(attempt_id)
            if heartbeat:
                self.state.update_attempt(
                    attempt_id, node=heartbeat.get("node", ""),
                    scratch_path=heartbeat.get("scratch_path", ""),
                )
                self.state.update_meta(
                    attempt["sample_id"],
                    compute_node=heartbeat.get("node", ""),
                    heartbeat_at=heartbeat.get("updated_at"),
                    heartbeat_age_s=heartbeat_age(heartbeat),
                    scratch_free_bytes=heartbeat.get(
                        "scratch_free_bytes"
                    ),
                )
            job_id = attempt.get("pbs_job_id")
            if not job_id:
                continue
            try:
                job = self.executor.status(job_id)
                self._last_stats_error = ""
                self._last_stats_ok_at = time.time()
            except Exception as exc:
                self._last_stats_error = str(exc)
                continue
            if job is None:
                # Completed PBS records may be purged before the manager sees
                # them. Give the worker event/Gluster commit time to appear.
                if time.time() - attempt["updated_at"] > 120:
                    node = (heartbeat or {}).get("node") or attempt.get(
                        "node", ""
                    )
                    scratch_path = (heartbeat or {}).get(
                        "scratch_path"
                    ) or attempt.get("scratch_path", "")
                    self.state.update_attempt(
                        attempt_id, status="interrupted",
                        node=node, scratch_path=scratch_path,
                        error="PBS job disappeared without a worker result",
                        ended_at=time.time(),
                    )
                    self.state.update_meta(
                        attempt["sample_id"], compute_node=node,
                        retained_scratch=scratch_path, retry_affinity=node,
                        pbs_state="missing", archive_pending=False,
                    )
                    self.state.upsert_sample(
                        attempt["sample_id"], status="failed", stage="pbs",
                        error=(
                            "PBS job disappeared without a worker result; "
                            "scratch was preserved for same-node retry"
                        ),
                    )
                continue
            status = job.state
            if status == "finished" and job.exit_code not in (None, 0):
                self._fail(
                    attempt,
                    f"PBS job exited with status {job.exit_code} "
                    "without a successful worker event",
                    {"exit_code": job.exit_code, "node": job.node,
                     "stage": (heartbeat or {}).get("stage", "pbs"),
                     "scratch_path": (heartbeat or {}).get(
                         "scratch_path", ""
                     )},
                )
                continue
            attempt_status = "running" if status == "running" else "queued"
            if status == "finished":
                attempt_status = "archiving"
            self.state.update_attempt(
                attempt_id, status=attempt_status,
                pbs_state=job.raw_state or status, node=job.node,
                exit_code=job.exit_code,
            )
            stage = (
                heartbeat.get("stage") if heartbeat
                else "archive_pending" if status == "finished"
                else "pbs_running" if status == "running"
                else "pbs_queued"
            )
            self.state.update_meta(
                attempt["sample_id"], pbs_state=job.raw_state or status,
                pbs_exit_code=job.exit_code, compute_node=job.node
                or (heartbeat or {}).get("node", ""),
                archive_pending=(status == "finished"),
            )
            self.state.upsert_sample(
                attempt["sample_id"], status="active", stage=stage, error=""
            )

    def _manifest_document(self, sample: dict, attempt_id: str,
                           scratch_bytes: int) -> dict:
        catalog_path = Path(self.cluster["artifacts_catalog"])
        catalog = load_catalog(catalog_path)
        artifacts = catalog["artifacts"]
        required = {"cellranger", "reference", "sratools", "crbase", "velocyto"}
        missing = sorted(required - set(artifacts))
        if missing:
            raise RuntimeError(
                "artifact catalog is missing: " + ", ".join(missing)
            )
        inputs = []
        records = sample["meta"].get("input_records", {})
        if sample["mode"] == "fastq":
            for record in records:
                inputs.append({
                    "kind": "fastq", "name": record["name"],
                    "path": record["path"], "size": int(record["size"]),
                    "sha256": record["sha256"],
                })
        else:
            for srr in sample["srrs"]:
                record = records.get(srr) or self.incoming.record_for(srr)
                if not record:
                    raise RuntimeError(f"ready input is missing for {srr}")
                inputs.append({
                    "kind": "sra", "srr": srr, "path": record["path"],
                    "size": int(record["size"]), "sha256": record["sha256"],
                })
        resources = {
            "cores": int(self.cfg.get(
                "concurrency", "cellranger_cores", default=32
            )),
            "memory_gb": int(self.cfg.get(
                "concurrency", "cellranger_mem_gb", default=128
            )),
            "convert_threads": int(self.cfg.get(
                "concurrency", "convert_threads", default=8
            )),
            "scratch_bytes": scratch_bytes,
            "scratch_min_free_fraction": float(
                self.cluster.get("scratch_min_free_fraction", 0.15)
            ),
        }
        return {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "sample": sample["id"],
            "project": self.cfg.project,
            "mode": sample["mode"],
            "srrs": sample["srrs"],
            "inputs": inputs,
            "paths": {
                "shared_root": str(self.shared_root),
                "shared_mount": str(self._shared_mount()),
                "shared_identity": self.cluster["shared_identity"],
                "scratch_mount": str(
                    self.cluster.get("scratch_mount", "/ssd")
                ),
                "scratch_root": str(self.cluster["scratch_root"]),
                "scratch_identity": self.cluster["scratch_identity"],
                "archive_root": str(self.cfg.path("archive_root")),
                "archive_staging_root": str(
                    self.shared_root / "archive" / ".staging"
                ),
                "require_mountpoints": bool(
                    self.cluster.get("require_mountpoints", True)
                ),
            },
            "resources": resources,
            "artifacts": artifacts,
            "pipeline": {
                "create_bam": bool(self.cfg.get(
                    "pipeline", "create_bam", default=True
                )),
                "make_loom": bool(self.cfg.get(
                    "pipeline", "make_loom", default=True
                )),
                "chemistry": self.cfg.get(
                    "pipeline", "chemistry", default="auto"
                ),
                "read_maps": self.cfg.get(
                    "metadata", "read_maps", default={}
                ),
            },
            "metadata": {
                key: sample["meta"].get(key) for key in (
                    "gsm", "organism", "instrument", "library_strategy"
                )
            },
            "archive": {
                "compression": self.cluster.get(
                    "archive_compression", "zstd"
                )
            },
            "cleanup": {
                "remove_scratch_after_commit": True,
            },
            "runtime": {
                "apptainer": self.cluster.get(
                    "apptainer", {}
                ).get("executable", "apptainer")
            },
            "heartbeat_interval_s": int(
                self.cluster.get("heartbeat_interval_s", 30)
            ),
        }

    def dispatch(self, sample: dict) -> str:
        if self.state.active_attempt_for(sample["id"]):
            raise RuntimeError(f"{sample['id']} already has an active attempt")
        estimate_record = estimate(sample, self.cfg)
        archive_root = self.cfg.path("archive_root")
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_disk = disk_usage(archive_root)
        archive_reserve = float(
            self.cfg.get("space", "min_free_archive_gb", default=200)
        ) * 1024 ** 3
        if (not archive_disk.get("ok")
                or archive_disk.get("free", 0)
                < estimate_record["archive_needed"] + archive_reserve):
            raise RuntimeError(
                "Gluster archive does not have enough free space for "
                f"{sample['id']} plus the configured reserve"
            )
        scratch_bytes = math.ceil(
            (estimate_record["work_needed"]
             + estimate_record["archive_needed"]) * 1.2
        )
        attempt_id = uuid.uuid4().hex
        last = self.state.last_attempt_for(sample["id"])
        preferred = sample["meta"].get("retry_affinity", "")
        if not preferred and last and last.get("node"):
            preferred = last["node"]
        manifest_path = (
            self.shared_root / "control" / "manifests" / sample["id"]
            / f"{attempt_id}.json"
        )
        self.state.create_attempt(
            sample["id"], attempt_id, status="preparing",
            manifest_path=str(manifest_path), scratch_bytes=scratch_bytes,
            preferred_node=preferred,
        )
        logger = sample_logger(
            sample["id"], self.cfg.logs_dir / f"{sample['id']}.log"
        )
        event_id = self.state.event_start(sample["id"], "pbs")
        try:
            document = self._manifest_document(
                sample, attempt_id, scratch_bytes
            )
            digest = write_manifest(manifest_path, document)
            self.state.update_attempt(
                attempt_id, manifest_sha256=digest,
                manifest_path=str(manifest_path),
            )
            job_id = self.executor.submit(
                manifest_path, digest, preferred_node=preferred,
                scratch_bytes=scratch_bytes,
            )
            self.state.update_attempt(
                attempt_id, status="submitted", pbs_job_id=job_id,
                pbs_state="submitted",
            )
            self.state.update_meta(
                sample["id"], attempt_id=attempt_id, pbs_job_id=job_id,
                pbs_state="submitted", pbs_event_id=event_id,
                scratch_estimate_bytes=scratch_bytes,
                retry_affinity=preferred,
            )
            self.state.upsert_sample(
                sample["id"], status="active", stage="pbs_queued", error=""
            )
            logger.info(
                "submitted PBS job %s attempt %s (scratch %.1f GiB)",
                job_id, attempt_id, scratch_bytes / 1024 ** 3,
            )
            return job_id
        except Exception as exc:
            self.state.event_end(event_id, "failed", 1, str(exc))
            self.state.update_attempt(
                attempt_id, status="failed", error=str(exc),
                ended_at=time.time(),
            )
            self.state.upsert_sample(
                sample["id"], status="failed", stage="pbs", error=str(exc)
            )
            raise

    def tick(self):
        self.reconcile()
        reason = self.paused_reason()
        if reason:
            if reason == "paused from dashboard":
                return
            if not self._environment_error():
                self.resume()
            else:
                return
        else:
            error = self._environment_error()
            if error:
                self.pause(error)
                return
        capacity = int(self.cluster.get("max_running_jobs", 2))
        available = max(0, capacity - len(self.state.list_active_attempts()))
        if not available:
            return
        queued = [
            sample for sample in self.state.list_samples()
            if sample["status"] == "queued"
        ]
        queued.sort(key=lambda sample: sum(
            int(record.get("size", 0)) for record in (
                sample["meta"].get("input_records", {}).values()
                if isinstance(sample["meta"].get("input_records", {}), dict)
                else sample["meta"].get("input_records", [])
            )
        ))
        for sample in queued[:available]:
            try:
                self.dispatch(sample)
            except Exception as exc:
                log.error("could not dispatch %s: %s", sample["id"], exc)

    def recover(self):
        self.reconcile()

    def retry(self, sample_id: str) -> str:
        sample = self.state.get_sample(sample_id)
        if not sample:
            return f"unknown sample {sample_id}"
        if sample["status"] != "failed":
            return f"sample {sample_id} is {sample['status']}; only failed samples can be retried"
        if self.state.active_attempt_for(sample_id):
            return f"sample {sample_id} still has an active PBS attempt"
        last = self.state.last_attempt_for(sample_id)
        if last and last.get("node"):
            self.state.update_meta(
                sample_id, retry_affinity=last["node"],
                retained_scratch=last.get("scratch_path", ""),
            )
        self.state.upsert_sample(
            sample_id, status="queued", stage="ready", error=""
        )
        return ""

    def cancel(self, sample_id: str) -> str:
        attempt = self.state.active_attempt_for(sample_id)
        if not attempt:
            return f"sample {sample_id} has no active PBS attempt"
        self.executor.cancel(attempt["pbs_job_id"])
        self.state.update_attempt(
            attempt["attempt_id"], status="cancelled",
            pbs_state="cancelled", ended_at=time.time(),
        )
        self.state.upsert_sample(
            sample_id, status="failed", stage="pbs",
            error="PBS job cancelled by operator; scratch was preserved",
        )
        return ""

    def cluster_state(self) -> dict:
        attempts = []
        stale_after = float(self.cluster.get("heartbeat_stale_s", 180))
        for attempt in self.state.list_active_attempts():
            heartbeat = self._heartbeat(attempt["attempt_id"])
            age = heartbeat_age(heartbeat)
            record = dict(attempt)
            record["heartbeat"] = heartbeat
            record["heartbeat_age_s"] = age
            record["heartbeat_stale"] = age is None or age > stale_after
            attempts.append(record)
        return {
            "mode": "pbs",
            "shared_root": str(self.shared_root),
            "active_attempts": attempts,
            "backup_warning": self.state.kv_get("cluster_backup_warning"),
            "max_running_jobs": int(
                self.cluster.get("max_running_jobs", 2)
            ),
        }

    def disks_snapshot(self):
        return {
            "manager": disk_usage(self.cfg.state_db.parent),
            "incoming": disk_usage(self.incoming.ready),
            "archive": disk_usage(self.cfg.path("archive_root")),
        }

    def job_stats(self):
        jobs = {}
        for attempt in self.state.list_active_attempts():
            heartbeat = self._heartbeat(attempt["attempt_id"]) or {}
            name = attempt.get("pbs_job_id") or attempt["attempt_id"]
            jobs[name] = {
                "sample": attempt["sample_id"],
                "state": attempt.get("pbs_state", ""),
                "node": heartbeat.get("node") or attempt.get("node", ""),
                "stage": heartbeat.get("stage", ""),
                "scratch_free_bytes": heartbeat.get(
                    "scratch_free_bytes"
                ),
            }
        return jobs

    def job_stats_health(self):
        jobs = self.job_stats()
        return {
            "last_ok_at": self._last_stats_ok_at,
            "error": self._last_stats_error,
            "stale": bool(self._last_stats_error),
            "jobs": jobs,
        }

    def resource_info(self):
        return {"memory_total": None, "cpus": None}

    def run_forever(self, scan_interval=60, tick_interval=10):
        self.recover()
        self.scan_inbox()
        last_scan = time.time()
        while not self._stop:
            try:
                self.tick()
                if time.time() - last_scan >= scan_interval:
                    self.scan_inbox()
                    last_scan = time.time()
            except Exception as exc:
                log.error("cluster manager tick failed: %s", exc)
            end = time.time() + tick_interval
            while not self._stop and time.time() < end:
                time.sleep(min(0.25, end - time.time()))

    def stop(self):
        # Deliberately do not qdel running jobs. PBS workers finish and the
        # restarted manager reconciles their heartbeats and archive commits.
        self._stop = True
