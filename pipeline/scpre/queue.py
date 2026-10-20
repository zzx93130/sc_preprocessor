"""Sample queue: registration from the inbox + worker scheduler.

Legacy mode bounds max_active_samples workers. Independent-lane mode bounds
downloads separately from the entire post-download sample lifecycle.
Drive/Docker outages pause the whole queue instead of failing samples.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import sra_table, stages
from .docker_run import make_runner
from .space import SpaceError, preflight
from .stages import (PauseError, ShutdownError, StageContext, clean_partial,
                     run_sample)
from .util import app_logger, sample_logger, stable_size

log = app_logger()


class Queue:
    def __init__(self, cfg, state, runner=None):
        self.cfg = cfg
        self.state = state
        self.runner = runner or make_runner(cfg)
        cc = cfg.data["concurrency"]
        self.semaphores = {
            "cellranger": threading.Semaphore(int(cc["max_cellranger_jobs"])),
            "converting": threading.Semaphore(1),
        }
        self.workers = {}
        self._worker_lanes = {}
        self._space_reservations = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- registration ---------------------------------------------------
    def scan_inbox(self) -> int:
        """Register samples from SraRunTable + inbox contents. Returns #new."""
        root = self.cfg.root
        inbox = self.cfg.path("inbox_dir")
        table = sra_table.discover(
            root, inbox, self.cfg.get("metadata", "sra_run_table", default=""))
        if not table:
            return 0
        try:
            groups = sra_table.parse(table)
        except ValueError as e:
            log.warning("SraRunTable parse problem: %s", e)
            return 0
        process_all = bool(self.cfg.get("metadata", "process_all", default=False))
        new = 0
        for sid, grp in groups.items():
            if self.state.get_sample(sid):
                existing = self.state.get_sample(sid)
                sizes = grp["meta"].get("run_bytes", {})
                if sizes and existing["meta"].get("run_bytes") != sizes:
                    self.state.update_meta(sid, run_bytes=sizes)
                continue
            present = {}
            for srr in grp["srrs"]:
                p = stages.resolve_sra_inputs(self.cfg, srr)
                if p is not None:
                    present[srr] = p
            if not present and not process_all:
                continue  # nothing downloaded for this sample yet
            # wait for size-stability of freshly arriving files
            unstable = [p for p in present.values() if not stable_size(p, 1.0)]
            if unstable:
                log.info("sample %s: inputs still changing, will re-scan", sid)
                continue
            meta = dict(grp["meta"])
            meta["input_paths"] = [str(p) for p in present.values()]
            meta["sra_paths"] = {k: str(v) for k, v in present.items()}
            meta["sra_run_table"] = str(table)
            self.state.upsert_sample(
                sid, project=self.cfg.project, mode="sra",
                srrs=grp["srrs"], status="queued", stage="queued", meta=meta)
            log.info("registered sample %s (%d runs, %d local)",
                     sid, len(grp["srrs"]), len(present))
            new += 1
        return new

    def add_fastq_sample(self, sample_id: str, fastq_paths, meta=None) -> None:
        m = dict(meta or {})
        m["input_paths"] = [str(Path(p).resolve()) for p in fastq_paths]
        self.state.upsert_sample(
            sample_id, project=self.cfg.project, mode="fastq", srrs=[],
            status="queued", stage="queued", meta=m)
        log.info("registered FASTQ sample %s (%d files)",
                 sample_id, len(m["input_paths"]))

    # ---- pause management ----------------------------------------------
    def paused_reason(self):
        return self.state.kv_get("paused_reason")

    def pause(self, reason: str) -> None:
        if self.paused_reason() == reason:
            return
        self.state.kv_set("paused_reason", reason)
        log.warning("QUEUE PAUSED: %s", reason)

    def resume(self) -> None:
        self.state.kv_set("paused_reason", None)
        log.info("queue resumed")

    def _environment_ok(self):
        from .util import disk_usage, path_available
        if not path_available(self.cfg.path("work_root")):
            return f"working drive unavailable: {self.cfg.path('work_root')}"
        work_disk = disk_usage(self.cfg.path("work_root"))
        if work_disk.get("inodes_total", 0) > 0 and \
                work_disk.get("inodes_free", 0) <= 0:
            return ("working filesystem has no free inodes; use APFS/ext4 "
                    "rather than exFAT for Cell Ranger work")
        if not path_available(self.cfg.path("archive_root")):
            return f"archive drive unavailable: {self.cfg.path('archive_root')}"
        if self.cfg.get("tools", "mode") == "docker":
            if not self.runner.healthy():
                return "Docker daemon unavailable"
            mount_err = self.runner.mounts_healthy([
                self.cfg.path("cellranger_dir"),
                self.cfg.path("reference_dir"),
                self.cfg.path("inbox_dir"),
            ])
            if mount_err:
                return ("Docker cannot access the reconnected drive mounts; "
                        f"restart Docker Desktop ({mount_err})")
        return None

    # ---- deferred cleanup ----------------------------------------------
    def cleanup_state(self) -> dict:
        pending = [s["id"] for s in self.state.list_samples()
                   if s["meta"].get("cleanup_pending")]
        status = self.state.kv_get("cleanup_status")
        if pending and not status:
            status = "Verified temporary files are waiting for Docker-idle cleanup"
        return {"pending_samples": pending, "status": status}

    def _set_cleanup_status(self, message=None) -> None:
        if self.state.kv_get("cleanup_status") == message:
            return
        self.state.kv_set("cleanup_status", message)
        if message:
            log.warning("CLEANUP WAITING: %s", message)
        else:
            log.info("deferred cleanup queue cleared")

    def _maintain_deferred_cleanup(self) -> bool:
        """Finalize verified work trees at a Docker-idle maintenance barrier.

        Return True when normal scheduling may continue.  Pending cleanup is
        intentionally a barrier: allowing more samples to start could fill the
        disk with visible pending trees before Docker releases the older bind
        mounts.
        """
        all_pending = [s for s in self.state.list_samples()
                       if s["meta"].get("cleanup_pending")]
        if not all_pending:
            if self.state.kv_get("cleanup_status"):
                self._set_cleanup_status(None)
            return True

        with self._lock:
            active = sorted(self.workers)
        if active:
            self._set_cleanup_status(
                "Waiting for active pipeline jobs before safely releasing "
                "temporary files: " + ", ".join(active))
            return False
        if self._stop.is_set():
            return False

        # stage_cleaning records cleanup_pending immediately before its stage
        # sentinel and final ``done`` state are committed.  A crash in that
        # narrow window must resume the checkpoint first; deleting the tree
        # here would erase valid earlier-stage sentinels and force a full rerun.
        pending = [s for s in all_pending
                   if s["status"] == "done" and s["stage"] == "done"]
        if not pending:
            self._set_cleanup_status(
                "A sample is resuming its final cleanup checkpoint; temporary "
                "files remain protected")
            return True

        restart_needed = any(
            not s["meta"].get("cleanup_docker_released_at") for s in pending)
        if (restart_needed
                and self.cfg.get("tools", "mode") == "docker"
                and self.runner.deferred_cleanup_requires_restart()):
            try:
                running = self.runner.running_containers()
            except Exception as e:  # noqa: BLE001
                self._set_cleanup_status(
                    f"Cannot verify Docker is idle; temporary files preserved ({e})")
                return False
            if running:
                self._set_cleanup_status(
                    "Temporary files preserved because Docker container(s) are "
                    "still running: " + ", ".join(running))
                return False
            self._set_cleanup_status(
                "Restarting idle Docker Desktop to release verified temporary files")
            try:
                self.runner.restart_desktop_for_cleanup()
            except Exception as e:  # noqa: BLE001
                self._set_cleanup_status(
                    f"Docker Desktop cleanup restart failed; files preserved ({e})")
                return False
            mount_err = self.runner.mounts_healthy([
                self.cfg.path("cellranger_dir"),
                self.cfg.path("reference_dir"),
                self.cfg.path("inbox_dir"),
            ])
            if mount_err:
                self._set_cleanup_status(
                    "Docker restarted but cannot access required drive mounts; "
                    f"temporary files preserved ({mount_err})")
                return False
            released_at = time.time()
            for sample in pending:
                self.state.update_meta(
                    sample["id"], cleanup_docker_released_at=released_at)

        completed = []
        for sample in pending:
            sid = sample["id"]
            logger = sample_logger(sid, self.cfg.logs_dir / f"{sid}.log")
            ctx = StageContext(self.cfg, self.state, self.runner,
                               self.state.get_sample(sid), logger)
            try:
                stages.finalize_deferred_cleanup(ctx)
                completed.append(sid)
            except Exception as e:  # noqa: BLE001
                self.state.update_meta(sid, cleanup_error=str(e)[:1000])
                self._set_cleanup_status(
                    f"Cleanup verification failed for {sid}; scheduling stopped ({e})")
                return False

        self._set_cleanup_status(None)
        log.info("reclaimed verified temporary work for %s",
                 ", ".join(completed))
        return True

    # ---- scheduling -----------------------------------------------------
    def _pause_check(self):
        if self.paused_reason():
            raise PauseError(self.paused_reason())

    def _worker(self, sample_id: str, lane=None) -> None:
        s = self.state.get_sample(sample_id)
        logger = sample_logger(sample_id, self.cfg.logs_dir / f"{sample_id}.log")
        ctx = StageContext(self.cfg, self.state, self.runner, s, logger)
        try:
            # Reserve the estimate atomically so two workers cannot both pass
            # preflight against the same bytes of free space.
            with self._lock:
                reserved_work = sum(
                    e["work_needed"] for e in self._space_reservations.values())
                reserved_archive = sum(
                    e["archive_needed"] for e in self._space_reservations.values())
                est = preflight(s, self.cfg, reserved_work, reserved_archive)
                self._space_reservations[sample_id] = est
            logger.info("space preflight OK (needs ~%.1f GB work, ~%.1f GB archive)",
                        est["work_needed"] / 1e9, est["archive_needed"] / 1e9)
            self.state.update_meta(sample_id, space_estimate=est)
            run_sample(ctx, self.semaphores, pause_check=self._pause_check,
                       stop_check=self._stop.is_set,
                       stage_names=["downloading"] if lane == "download" else None)
            if lane == "download":
                # No conversion in this lane. Publish the durable checkpoint
                # and release the download slot independently of compute.
                self.state.upsert_sample(sample_id, status="queued",
                                         stage="converting", error="")
        except ShutdownError:
            self.state.interrupt_open_events(
                sample_id, "interrupted by controlled service shutdown")
            self.state.upsert_sample(sample_id, status="queued", error="")
            logger.info("service stopping; sample safely requeued")
        except PauseError as e:
            if not self._stop.is_set():
                self.pause(str(e))
            self.state.upsert_sample(sample_id, status="queued", error="")
            logger.warning("paused mid-run (%s); sample requeued, progress kept", e)
        except SpaceError as e:
            self.state.upsert_sample(sample_id, status="failed", error=str(e))
            logger.error("%s", e)
        except OSError as e:
            # a drive vanished between the tick's env check and here
            self.pause(f"filesystem error (drive disconnected?): {e}")
            self.state.upsert_sample(sample_id, status="queued", error="")
            logger.warning("paused on filesystem error: %s", e)
        except Exception as e:  # noqa: BLE001  (already recorded by run_sample)
            if self._stop.is_set():
                self.state.interrupt_open_events(
                    sample_id, "interrupted by controlled service shutdown")
                self.state.upsert_sample(sample_id, status="queued", error="")
                logger.info("service stopped current tool; sample safely requeued")
            else:
                logger.error("sample failed: %s", e)
        finally:
            with self._lock:
                self.workers.pop(sample_id, None)
                self._worker_lanes.pop(sample_id, None)
                self._space_reservations.pop(sample_id, None)

    def _download_ready(self, sample):
        if sample["mode"] == "fastq":
            return True
        logger = sample_logger(sample["id"], self.cfg.logs_dir / f"{sample['id']}.log")
        ctx = StageContext(self.cfg, self.state, self.runner, sample, logger)
        return ctx.sentinel_valid("downloading")

    def _tick_lanes(self, queued):
        """Bound download-ahead separately; serialize whole preprocessing jobs.

        A preprocessing slot lasts through conversion, Cell Ranger, loom,
        archive verification and cleanup, not just each individual tool.
        """
        ready = [s for s in queued if self._download_ready(s)]
        ready_ids = {s["id"] for s in ready}
        missing = [s for s in queued if s["id"] not in ready_ids]
        with self._lock:
            # Adopted jobs and legacy workers conservatively occupy compute.
            compute = sum(self._worker_lanes.get(sid) != "download"
                          for sid in self.workers)
            downloads = len(self.workers) - compute
        compute_slots = max(0, int(self.cfg.get(
            "concurrency", "max_preprocessing_samples", default=1)) - compute)
        selected_ready = ready[:compute_slots]
        ahead = max(1, int(self.cfg.get("concurrency", "download_ahead", default=1)))
        download_slots = max(0, min(
            int(self.cfg.get("concurrency", "max_download_jobs", default=1)) - downloads,
            ahead - (len(ready) - len(selected_ready)) - downloads))
        for sample, lane in ([(s, "preprocess") for s in selected_ready]
                             + [(s, "download") for s in missing[:download_slots]]):
            sid = sample["id"]
            with self._lock:
                if sid in self.workers:
                    continue
                thread = threading.Thread(target=self._worker, args=(sid, lane),
                                          name=f"{lane}-{sid}", daemon=True)
                self.workers[sid] = thread
                self._worker_lanes[sid] = lane
            self.state.upsert_sample(sid, status="active", error="")
            thread.start()

    def tick(self) -> None:
        reason = self.paused_reason()
        if reason:
            # An operator pause is sticky.  Only environment-triggered pauses
            # auto-resume when Docker or a drive comes back.
            if reason == "paused from dashboard":
                return
            if self._environment_ok() is None:
                self.resume()
            else:
                return
        else:
            env_err = self._environment_ok()
            if env_err:
                self.pause(env_err)
                return
        if not self._maintain_deferred_cleanup():
            return
        max_active = int(self.cfg.get("concurrency", "max_active_samples", default=2))
        with self._lock:
            active = len(self.workers)
        separate = self.cfg.get("concurrency", "separate_downloads", default=False)
        if not separate and active >= max_active:
            return
        queued = [s for s in self.state.list_samples() if s["status"] == "queued"]
        # smallest inputs first so the first real run doubles as a smoke test
        def input_size(s):
            total = 0
            for p in s["meta"].get("input_paths", []):
                try:
                    total += Path(p).stat().st_size
                except OSError:
                    pass
            return total or float("inf")
        queued.sort(key=input_size)
        if separate:
            self._tick_lanes(queued)
            return
        for s in queued[: max_active - active]:
            with self._lock:
                if s["id"] in self.workers:
                    continue
                t = threading.Thread(target=self._worker, args=(s["id"],),
                                     name=f"worker-{s['id']}", daemon=True)
                self.workers[s["id"]] = t
            self.state.upsert_sample(s["id"], status="active")
            t.start()

    def recover(self) -> None:
        """Reconcile abandoned state and Docker jobs before scheduling work."""
        samples = self.state.list_samples()
        try:
            jobs = self.runner.list_jobs()
        except Exception as e:  # noqa: BLE001
            if self.cfg.get("tools", "mode") == "docker":
                self.pause(f"Docker recovery check failed: {e}")
                return
            jobs = []

        for s in samples:
            safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_"
                           for ch in s["id"])
            prefix = f"scpre-{safe}-"
            sample_jobs = [j for j in jobs if j["name"].startswith(prefix)]
            if sample_jobs:
                expected_cr = f"{prefix}cellranger"
                adoptable = (len(sample_jobs) == 1
                             and sample_jobs[0]["name"] == expected_cr
                             and sample_jobs[0]["state"] == "running"
                             and s["stage"] == "cellranger")
                if adoptable:
                    self._adopt_cellranger(s["id"], expected_cr)
                    continue
                log.warning("recovering %s: stopping abandoned job(s): %s",
                            s["id"], ", ".join(j["name"] for j in sample_jobs))
                errors = self.runner.stop_sample_jobs(s["id"])
                if errors:
                    msg = "Docker recovery failed: " + "; ".join(errors)
                    self.state.upsert_sample(s["id"], status="failed", error=msg)
                    log.error("%s: %s", s["id"], msg)
                    continue
                self.state.interrupt_open_events(
                    s["id"], "interrupted service job was stopped during recovery")
                self.state.upsert_sample(s["id"], status="queued", error="")
                continue

            # No Docker job survived for this sample, so every still-open
            # event necessarily belongs to an older service process.  This
            # also catches records whose sample was already set back to
            # ``queued`` before that process disappeared.
            closed = self.state.interrupt_open_events(
                s["id"], "service ended before the stage completed")
            if closed and s["status"] not in ("active", "waiting"):
                log.info("recovered %d abandoned event(s) for %s",
                         closed, s["id"])
            if s["status"] in ("active", "waiting"):
                log.info("recovering interrupted sample %s (stage %s)",
                         s["id"], s["stage"])
                self.state.upsert_sample(s["id"], status="queued", error="")

    def _adopt_cellranger(self, sample_id: str, job_name: str) -> None:
        """Monitor a Cell Ranger container that outlived the old service."""
        sem = self.semaphores["cellranger"]
        if not sem.acquire(blocking=False):
            # No scheduler workers exist during recover(), so this indicates
            # inconsistent state.  Leave normal orphan handling to the caller.
            raise RuntimeError("cannot reserve Cell Ranger slot for recovered job")

        logger = sample_logger(sample_id,
                               self.cfg.logs_dir / f"{sample_id}.log")

        def monitor():
            detail = "adopted Cell Ranger job finished after service restart"
            try:
                logger.warning("adopting surviving Docker job %s", job_name)
                exit_code = self.runner.wait_job(job_name)
                if exit_code:
                    detail = (f"adopted Cell Ranger job exited {exit_code} "
                              "after service restart")
                    logger.warning(detail)
                else:
                    logger.info("adopted Cell Ranger job exited successfully; "
                                "validating saved outputs")
            except Exception as e:  # noqa: BLE001
                detail = f"could not monitor adopted Cell Ranger job: {e}"
                logger.warning(detail)
            finally:
                self.state.interrupt_open_events(sample_id, detail)
                self.state.upsert_sample(sample_id, status="queued",
                                         stage="cellranger", error="")
                sem.release()
                with self._lock:
                    self.workers.pop(sample_id, None)

        thread = threading.Thread(target=monitor,
                                  name=f"adopt-{sample_id}", daemon=True)
        with self._lock:
            self.workers[sample_id] = thread
        self.state.upsert_sample(sample_id, status="active",
                                 stage="cellranger", error="")
        thread.start()

    def retry(self, sample_id: str) -> str:
        s = self.state.get_sample(sample_id)
        if not s:
            return f"unknown sample {sample_id}"
        if s["status"] not in ("failed", "done"):
            return f"sample {sample_id} is {s['status']}; only failed samples can be retried"
        if s["status"] == "done":
            return f"sample {sample_id} is done; nothing to retry"
        try:
            jobs = self.runner.sample_jobs(sample_id)
        except Exception as e:  # noqa: BLE001
            return f"cannot verify Docker state for {sample_id}: {e}"
        if jobs:
            return (f"refusing retry while Docker job(s) still exist: "
                    + ", ".join(j["name"] for j in jobs))
        logger = sample_logger(sample_id, self.cfg.logs_dir / f"{sample_id}.log")
        ctx = StageContext(self.cfg, self.state, self.runner, s, logger)
        # Docker exit 125 means the container process never started.  This is
        # common just after an external drive is reconnected, while Docker
        # Desktop still has a stale /host_mnt view.  Preserve all stage files
        # in that case—especially a resumable Cell Ranger pipestance left by
        # the earlier service—because no replacement tool modified them.
        err = s.get("error") or ""
        launch_failed = ("exit 125" in err
                         and "docker: Error response from daemon" in err)
        if launch_failed:
            logger.info("retrying Docker launch failure; stage files preserved")
            action = "preserved"
        else:
            clean_partial(ctx, s["stage"])
            action = "cleaned"
        self.state.upsert_sample(sample_id, status="queued", error="")
        logger.info("retry requested: stage %r %s, sample requeued",
                    s["stage"], action)
        return ""

    # ---- service loop ---------------------------------------------------
    def run_forever(self, scan_interval: float = 60, tick_interval: float = 5):
        self.recover()
        self.scan_inbox()
        last_scan = time.time()
        while not self._stop.is_set():
            try:
                self.tick()
                if time.time() - last_scan >= scan_interval:
                    self.scan_inbox()
                    last_scan = time.time()
            except Exception as e:  # noqa: BLE001
                log.error("queue tick error: %s", e)
            self._stop.wait(tick_interval)

    def stop(self):
        self._stop.set()
        self.runner.begin_shutdown()
        with self._lock:
            sample_ids = list(self.workers)
        for sample_id in sample_ids:
            try:
                errors = self.runner.stop_sample_jobs(sample_id)
                for err in errors:
                    log.error("shutdown: %s", err)
            except Exception as e:  # noqa: BLE001
                log.error("shutdown could not stop %s Docker jobs: %s",
                          sample_id, e)
        deadline = time.time() + 60
        for sample_id in sample_ids:
            with self._lock:
                worker = self.workers.get(sample_id)
            if worker and worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.time()))
        # Close the small race where shutdown begins immediately before a
        # worker's docker run call. begin_shutdown blocks future starts; this
        # second sweep catches any process that crossed the boundary already.
        for sample_id in sample_ids:
            try:
                self.runner.stop_sample_jobs(sample_id)
            except Exception as e:  # noqa: BLE001
                log.error("final shutdown sweep for %s failed: %s", sample_id, e)
        # Docker Desktop can restart or become unreachable while a container
        # is being stopped.  Persist a restart-safe state even in that case;
        # the next recover() pass reconciles and stops any surviving job before
        # a replacement can start.
        for sample_id in sample_ids:
            self.state.interrupt_open_events(
                sample_id, "interrupted by controlled service shutdown")
            s = self.state.get_sample(sample_id)
            if s and s["status"] in ("active", "waiting"):
                self.state.upsert_sample(sample_id, status="queued", error="")
