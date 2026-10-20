"""FastAPI dashboard + JSON API (contract from the sra-auto-pipeline skill).

Endpoints: /api/state, /api/logs/<sample>, /api/disk, POST /api/retry/<sample>,
plus /api/metrics, pause/resume, and safe file links for results.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .space import disks_snapshot, same_filesystem
from .util import app_logger

log = app_logger()

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

WEB_DIR = Path(__file__).parent / "web"


def _check_id(sample_id: str) -> str:
    if not SAFE_ID.match(sample_id):
        raise HTTPException(400, "bad sample id")
    return sample_id


class MetricsSampler(threading.Thread):
    def __init__(self, cfg, state, runner, interval: float = 5.0):
        super().__init__(name="metrics", daemon=True)
        self.cfg, self.state, self.runner = cfg, state, runner
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self):
        last_prune = 0.0
        psutil.cpu_percent(None)  # prime
        while not self._stop_event.is_set():
            try:
                cpu = psutil.cpu_percent(None)
                vm = psutil.virtual_memory()
                disks = disks_snapshot(self.cfg)
                jobs = {}
                try:
                    jobs = self.runner.job_stats()
                except Exception:  # noqa: BLE001
                    pass
                self.state.metrics_insert(cpu, vm.used, vm.total, disks, jobs)
                if time.time() - last_prune > 3600:
                    self.state.metrics_prune()
                    last_prune = time.time()
            except Exception as e:  # noqa: BLE001
                log.warning("metrics sampler: %s", e)
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()


def _sample_view(cfg, state, s: dict) -> dict:
    events = state.events_for(s["id"])
    stages = []
    for ev in events:
        dur = None
        if ev["ended_at"] and ev["started_at"]:
            dur = round(ev["ended_at"] - ev["started_at"], 1)
        stages.append({"stage": ev["stage"], "status": ev["status"],
                       "started_at": ev["started_at"], "ended_at": ev["ended_at"],
                       "duration_s": dur, "detail": ev["detail"] or ""})
    meta = s["meta"]
    arch = cfg.archive_sample_dir(s["id"])
    work_ws = cfg.work_dir / s["id"] / "cellranger" / s["id"] / "outs" / "web_summary.html"
    arch_ws = arch / "results" / "outs" / "web_summary.html"
    has_ws = work_ws.is_file() or arch_ws.is_file()
    loom_work = cfg.work_dir / s["id"] / "loom" / f"{s['id']}.loom"
    loom_arch = arch / "loom" / f"{s['id']}.loom"
    has_loom = loom_work.is_file() or loom_arch.is_file()
    return {
        "id": s["id"], "project": s["project"], "mode": s["mode"],
        "srrs": s["srrs"], "status": s["status"], "stage": s["stage"],
        "error": s["error"], "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "meta": {k: meta.get(k) for k in
                 ("gsm", "organism", "instrument", "read_counts",
                   "cellranger_version", "reference", "archive_dir",
                   "space_estimate", "library_strategy", "cleanup_pending",
                   "cleanup_requested_at", "cleanup_completed_at",
                   "cleanup_docker_released_at", "cleanup_error",
                   "attempt_id", "pbs_job_id", "compute_node", "pbs_state",
                   "pbs_exit_code", "heartbeat_at", "heartbeat_age_s",
                   "scratch_free_bytes", "scratch_estimate_bytes",
                   "retry_affinity", "retained_scratch", "archive_pending")},
        "stage_events": stages,
        "links": {
            "log": f"/api/logs/{s['id']}",
            "log_file": f"/files/log/{s['id']}",
            "web_summary": f"/files/web_summary/{s['id']}" if has_ws else None,
            "loom": f"/files/loom/{s['id']}" if has_loom else None,
            "outs": f"/files/outs/{s['id']}",
        },
    }


def create_app(cfg, state, queue, runner) -> FastAPI:
    app = FastAPI(title="scpre dashboard")
    started_at = time.time()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def api_state():
        samples = [_sample_view(cfg, state, s) for s in state.list_samples()]
        warnings = []
        reason = queue.paused_reason()
        if reason:
            warnings.append({"level": "error", "message": f"Queue paused: {reason}"})
        cleanup = queue.cleanup_state()
        if cleanup.get("status"):
            warnings.append({"level": "warn", "message": cleanup["status"]})
        cluster_mode = callable(getattr(queue, "cluster_state", None))
        cluster = queue.cluster_state() if cluster_mode else None
        if cluster and cluster.get("backup_warning"):
            warnings.append({
                "level": "error", "message": cluster["backup_warning"]
            })
        for attempt in (cluster or {}).get("active_attempts", []):
            if (attempt.get("status") in ("running", "archiving")
                    and attempt.get("heartbeat_stale")):
                warnings.append({
                    "level": "warn",
                    "message": (
                        f"{attempt['sample_id']} has a stale worker heartbeat "
                        f"for PBS job {attempt.get('pbs_job_id') or '?'}"
                    ),
                })
        disks = (
            queue.disks_snapshot() if callable(
                getattr(queue, "disks_snapshot", None)
            ) else disks_snapshot(cfg)
        )
        for name, d in disks.items():
            if d.get("ok") and d["total"] and d["free"] / d["total"] < 0.08:
                warnings.append({"level": "warn",
                                 "message": f"{name} disk under 8% free"})
            if not d.get("ok"):
                warnings.append({"level": "error",
                                 "message": f"{name} drive unavailable: {d.get('error', '')}"})
        if not cluster_mode and same_filesystem(
                cfg.path("work_root"), cfg.path("archive_root")):
            warnings.append({
                "level": "warn",
                "message": "Work and archive share one filesystem; space checks combine both footprints."})

        stats_health = runner.job_stats_health()
        if stats_health.get("stale"):
            warnings.append({
                "level": "warn",
                "message": (
                    "PBS job information is stale; showing the last successful "
                    "sample." if cluster_mode else
                    "Docker job metrics are stale; showing the last successful sample."
                )})
        job_names = set(stats_health.get("jobs", {}))
        worker_ids = set(queue.workers)
        for s in samples if not cluster_mode else []:
            safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_"
                           for ch in s["id"])
            has_container = any(n.startswith(f"scpre-{safe}-") for n in job_names)
            if has_container and s["status"] not in ("active", "waiting"):
                warnings.append({
                    "level": "error",
                    "message": f"{s['id']} has a Docker container but database status is {s['status']}."})
            if s["status"] in ("active", "waiting") and s["id"] not in worker_ids:
                warnings.append({
                    "level": "error",
                    "message": f"{s['id']} is marked {s['status']} but has no scheduler worker."})

        resources = runner.resource_info()
        docker_mem = resources.get("memory_total")
        docker_cpus = resources.get("cpus")
        try:
            requested_mem = int(
                cfg.get("concurrency", "cellranger_mem_gb", default=0))
        except (TypeError, ValueError):
            requested_mem = 0
        if not cluster_mode and docker_mem and requested_mem * 1024 ** 3 > docker_mem * 0.85:
            warnings.append({
                "level": "warn",
                "message": (f"Cell Ranger requests {requested_mem} GB of a "
                            f"{docker_mem / 1024 ** 3:.1f} GiB Docker VM; leave more headroom.")})
        try:
            requested_cores = int(
                cfg.get("concurrency", "cellranger_cores", default=0))
        except (TypeError, ValueError):
            requested_cores = 0
        if not cluster_mode and docker_cpus and requested_cores > docker_cpus:
            warnings.append({
                "level": "error",
                "message": (f"Cell Ranger requests {requested_cores} cores but "
                            f"the Docker VM has only {docker_cpus} CPUs.")})
        return {
            "project": cfg.project,
            "server_started_at": started_at,
            "paused": bool(reason),
            "paused_reason": reason,
            "cleanup": cleanup,
            "active_workers": sorted(queue.workers.keys()),
            "docker_stats": stats_health,
            "docker_resources": resources,
            "samples": samples,
            "disks": disks,
            "warnings": warnings,
            "cluster": cluster,
            "config": {
                "reference": str(cfg.path("reference_dir")),
                "cellranger": str(cfg.path("cellranger_dir")),
                "inbox": str(cfg.path("inbox_dir")),
                "archive": str(cfg.path("archive_root")),
                "work": str(cfg.path("work_root")),
                "concurrency": cfg.data["concurrency"],
                "tools_mode": cfg.get("tools", "mode"),
            },
        }

    @app.get("/api/disk")
    def api_disk():
        return disks_snapshot(cfg)

    @app.get("/api/metrics")
    def api_metrics(minutes: int = 30):
        return state.metrics_recent(minutes * 60)

    @app.get("/api/logs/{sample_id}")
    def api_logs(sample_id: str, tail: int = 200):
        _check_id(sample_id)
        p = cfg.logs_dir / f"{sample_id}.log"
        if not p.is_file():
            raise HTTPException(404, "no log yet")
        lines = p.read_text(errors="replace").splitlines()[-max(1, min(tail, 5000)):]
        s = state.get_sample(sample_id)
        return {"sample": sample_id, "stage": s["stage"] if s else None,
                "lines": lines}

    @app.post("/api/retry/{sample_id}")
    def api_retry(sample_id: str):
        _check_id(sample_id)
        err = queue.retry(sample_id)
        if err:
            raise HTTPException(409, err)
        return {"ok": True, "sample": sample_id}

    @app.post("/api/pause")
    def api_pause():
        queue.pause("paused from dashboard")
        return {"ok": True}

    @app.post("/api/resume")
    def api_resume():
        queue.resume()
        return {"ok": True}

    @app.post("/api/scan")
    def api_scan():
        return {"ok": True, "new": queue.scan_inbox()}

    # ---- file links -----------------------------------------------------
    @app.get("/files/log/{sample_id}")
    def file_log(sample_id: str):
        _check_id(sample_id)
        p = cfg.logs_dir / f"{sample_id}.log"
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="text/plain")

    def _first_existing(*paths):
        for p in paths:
            if p.is_file():
                return p
        return None

    @app.get("/files/web_summary/{sample_id}")
    def file_web_summary(sample_id: str):
        _check_id(sample_id)
        p = _first_existing(
            cfg.work_dir / sample_id / "cellranger" / sample_id / "outs" / "web_summary.html",
            cfg.archive_sample_dir(sample_id) / "results" / "outs" / "web_summary.html")
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="text/html")

    @app.get("/files/loom/{sample_id}")
    def file_loom(sample_id: str):
        _check_id(sample_id)
        p = _first_existing(
            cfg.work_dir / sample_id / "loom" / f"{sample_id}.loom",
            cfg.archive_sample_dir(sample_id) / "loom" / f"{sample_id}.loom")
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="application/octet-stream",
                            filename=p.name)

    @app.get("/files/outs/{sample_id}")
    def file_outs(sample_id: str):
        _check_id(sample_id)
        for base in (cfg.work_dir / sample_id / "cellranger" / sample_id / "outs",
                     cfg.archive_sample_dir(sample_id) / "results" / "outs"):
            if base.is_dir():
                files = []
                for p in sorted(base.rglob("*")):
                    if p.is_file():
                        files.append({"path": str(p.relative_to(base)),
                                      "size": p.stat().st_size})
                return {"sample": sample_id, "location": str(base), "files": files}
        raise HTTPException(404)

    @app.get("/files/outs/{sample_id}/get")
    def file_outs_get(sample_id: str, path: str):
        _check_id(sample_id)
        if ".." in path or path.startswith("/"):
            raise HTTPException(400)
        for base in (cfg.work_dir / sample_id / "cellranger" / sample_id / "outs",
                     cfg.archive_sample_dir(sample_id) / "results" / "outs"):
            p = (base / path)
            try:
                p.relative_to(base)
            except ValueError:
                raise HTTPException(400)
            if p.is_file():
                return FileResponse(p)
        raise HTTPException(404)

    @app.exception_handler(Exception)
    def on_error(request, exc):
        log.error("API error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return app


def serve(cfg, state, queue, runner):
    import uvicorn
    sampler = MetricsSampler(cfg, state, runner)
    sampler.start()
    qthread = threading.Thread(target=queue.run_forever, name="queue", daemon=True)
    qthread.start()
    app = create_app(cfg, state, queue, runner)
    host = cfg.get("dashboard", "host", default="127.0.0.1")
    port = int(cfg.get("dashboard", "port", default=9123))
    log.info("dashboard: http://%s:%d", host, port)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        queue.stop()
        sampler.stop()
