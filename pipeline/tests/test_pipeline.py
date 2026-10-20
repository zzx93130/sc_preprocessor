import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from scpre import sra_table
from scpre.docker_run import DockerRunner, MockRunner, container_name
from scpre.dryrun import build_fixture_project, run_dry_run
from scpre.queue import Queue
from scpre.space import SpaceError, preflight
from scpre.state import State


# --- SraRunTable ----------------------------------------------------------

def test_sra_table_grouping(tmp_path):
    t = tmp_path / "SraRunTable.csv"
    t.write_text(
        "Run,Sample Name,GEO_Accession (exp),Organism\n"
        "SRR1,GSM10,GSM10,Homo sapiens\n"
        "SRR2,GSM10,GSM10,Homo sapiens\n"
        "SRR3,GSM11,GSM11,Homo sapiens\n")
    groups = sra_table.parse(t)
    assert set(groups) == {"GSM10", "GSM11"}
    assert groups["GSM10"]["srrs"] == ["SRR1", "SRR2"]
    assert groups["GSM10"]["meta"]["organism"] == "Homo sapiens"


def test_sra_table_no_gsm_falls_back_to_sample_name(tmp_path):
    t = tmp_path / "SraRunTable.csv"
    t.write_text("Run,Sample Name\nSRR9,my sample A\n")
    groups = sra_table.parse(t)
    assert list(groups) == ["my_sample_A"]


def test_read_map_parse():
    m = sra_table.parse_read_map("1=I1,2=R1,3=R2")
    assert m == {1: "I1", 2: "R1", 3: "R2"}
    with pytest.raises(ValueError):
        sra_table.parse_read_map("1=XX")


# --- full dry-run ---------------------------------------------------------

def test_dry_run_end_to_end(tmp_path):
    assert run_dry_run(keep=True, base_dir=str(tmp_path / "dr")) == 0


# --- helpers for queue-level tests ---------------------------------------

def make_env(tmp_path, fail_stage=None):
    cfg = build_fixture_project(tmp_path)
    state = State(cfg.state_db)
    runner = MockRunner(cfg, fail_stage=fail_stage)
    q = Queue(cfg, state, runner=runner)
    return cfg, state, q


def drain(q, state, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        q.tick()
        samples = state.list_samples()
        if samples and all(s["status"] in ("done", "failed") for s in samples) \
                and not q.workers:
            return samples
        time.sleep(0.1)
    raise AssertionError("queue did not drain: "
                         + str([(s['id'], s['status']) for s in state.list_samples()]))


# --- failure + retry ------------------------------------------------------

def test_failure_and_retry_resumes_without_redoing(tmp_path):
    cfg, state, q = make_env(tmp_path, fail_stage="cellranger")
    q.scan_inbox()
    samples = drain(q, state)
    assert all(s["status"] == "failed" for s in samples)
    assert all(s["stage"] == "cellranger" for s in samples)

    sid = samples[0]["id"]
    conv_sentinel = cfg.work_dir / sid / ".stages" / "converting.json"
    assert conv_sentinel.exists()
    conv_mtime = conv_sentinel.stat().st_mtime

    # heal the tool, retry every failed sample
    q.runner.fail_stage = None
    for s in samples:
        assert q.retry(s["id"]) == ""
    samples = drain(q, state)
    assert all(s["status"] == "done" for s in samples)
    # converting was NOT re-run (sentinel untouched)
    assert conv_sentinel.stat().st_mtime == conv_mtime
    # retry on a done sample is refused
    assert "done" in q.retry(sid)


def test_restart_recovers_interrupted_sample(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.upsert_sample(sid, status="active", stage="converting")  # simulate crash
    q2 = Queue(cfg, state, runner=MockRunner(cfg))
    q2.recover()
    assert state.get_sample(sid)["status"] == "queued"
    drain(q2, state)
    assert state.get_sample(sid)["status"] == "done"


def test_conversion_restart_reuses_completed_uncompressed_reads(tmp_path):
    from scpre.stages import StageContext, stage_converting
    from scpre.util import sample_logger

    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sample = state.list_samples()[0]
    ctx = StageContext(cfg, state, q.runner, sample,
                       sample_logger(sample["id"],
                                     cfg.logs_dir / f"{sample['id']}.log"))
    ctx.fastq_dir.mkdir(parents=True)
    for lane, _srr in enumerate(sample["srrs"], start=1):
        for role in ("R1", "R2"):
            path = ctx.fastq_dir / (
                f"{ctx.sid}_S1_L{lane:03d}_{role}_001.fastq"
            )
            q.runner._write_fastq(
                path, 10, 28 if role == "R1" else 90, lane,
            )
    q.runner.fasterq = lambda *args, **kwargs: pytest.fail(
        "fasterq must not rerun after reads were staged"
    )

    outputs = stage_converting(ctx)

    assert len(outputs) == 2 * len(sample["srrs"])
    assert all(Path(path).suffix == ".gz" for path in outputs)
    assert all(Path(path).is_file() for path in outputs)


def test_interrupted_cellranger_resumes_existing_pipestance(tmp_path):
    from scpre.stages import StageContext, stage_cellranger
    from scpre.util import sample_logger

    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sample = state.list_samples()[0]
    ctx = StageContext(cfg, state, q.runner, sample,
                       sample_logger(sample["id"],
                                     cfg.logs_dir / f"{sample['id']}.log"))
    ctx.fastq_dir.mkdir(parents=True)
    (ctx.fastq_dir / f"{ctx.sid}_S1_L001_R1_001.fastq.gz").write_bytes(b"fq")
    pipestance = ctx.cr_dir / ctx.sid
    pipestance.mkdir(parents=True)
    (pipestance / "_invocation").write_text("interrupted")
    (pipestance / "_lock").write_text("stale")
    marker = pipestance / "completed-chunk"
    marker.write_text("keep")

    original = q.runner.cellranger_count

    def assert_preserved(*args, **kwargs):
        assert marker.is_file()
        assert not (pipestance / "_lock").exists()
        return original(*args, **kwargs)

    q.runner.cellranger_count = assert_preserved
    stage_cellranger(ctx)

    assert marker.read_text() == "keep"
    assert (ctx.outs / "web_summary.html").is_file()


def test_restart_stops_orphan_and_closes_open_event(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.upsert_sample(sid, status="active", stage="converting")
    state.event_start(sid, "converting")
    name = container_name(sid, "fasterq-SRR900001")
    q.runner.live_jobs[name] = {
        "name": name, "state": "running", "status": "Up 10 minutes"}

    q.recover()

    assert not q.runner.live_jobs
    assert state.get_sample(sid)["status"] == "queued"
    event = state.events_for(sid)[-1]
    assert event["status"] == "interrupted"
    assert event["ended_at"] is not None


def test_restart_adopts_surviving_cellranger_job(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.upsert_sample(sid, status="active", stage="cellranger")
    state.event_start(sid, "cellranger")
    name = container_name(sid, "cellranger")
    q.runner.live_jobs[name] = {
        "name": name, "state": "running", "status": "Up 10 minutes"}
    waited = threading.Event()

    def finish_job(job_name):
        assert job_name == name
        waited.set()
        q.runner.live_jobs.pop(name)
        return 0

    q.runner.wait_job = finish_job
    q.recover()

    assert waited.wait(2)
    deadline = time.time() + 2
    while time.time() < deadline and sid in q.workers:
        time.sleep(0.01)
    assert sid not in q.workers
    assert state.get_sample(sid)["status"] == "queued"
    event = state.events_for(sid)[-1]
    assert event["status"] == "interrupted"
    assert "adopted Cell Ranger job finished" in event["detail"]


def test_restart_closes_open_event_even_if_sample_was_already_queued(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.upsert_sample(sid, status="queued", stage="converting")
    state.event_start(sid, "converting")

    q.recover()

    assert state.get_sample(sid)["status"] == "queued"
    event = state.events_for(sid)[-1]
    assert event["status"] == "interrupted"
    assert event["ended_at"] is not None


def test_retry_refuses_to_clean_while_container_exists(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.upsert_sample(sid, status="failed", stage="converting")
    marker = cfg.work_dir / sid / "raw_fastq" / "keep-me"
    marker.parent.mkdir(parents=True)
    marker.write_text("partial")
    name = container_name(sid, "fasterq-SRR900001")
    q.runner.live_jobs[name] = {
        "name": name, "state": "running", "status": "Up 1 minute"}

    err = q.retry(sid)

    assert "refusing retry" in err
    assert marker.is_file()


def test_retry_preserves_cellranger_after_docker_launch_failure(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    marker = cfg.work_dir / sid / "cellranger" / sid / "completed-chunk"
    marker.parent.mkdir(parents=True)
    marker.write_text("preserve")
    state.upsert_sample(
        sid, status="failed", stage="cellranger",
        error=("cellranger failed (exit 125): docker: Error response from "
               "daemon: failed to fulfil mount request"))

    assert q.retry(sid) == ""

    assert marker.read_text() == "preserve"
    assert state.get_sample(sid)["status"] == "queued"


def test_retry_cleans_cellranger_work_volume_before_host_partial(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    marker = cfg.work_dir / sid / "cellranger" / sid / "partial"
    marker.parent.mkdir(parents=True)
    marker.write_text("failed pipestance")
    calls = []
    q.runner.clean_partial = lambda sample, stage: calls.append((sample, stage))
    state.upsert_sample(sid, status="failed", stage="cellranger",
                        error="preflight failed")

    assert q.retry(sid) == ""

    assert calls == [(sid, "cellranger")]
    assert not marker.exists()


@pytest.mark.parametrize("with_overrides", [False, True])
def test_cellranger_uses_linux_volume_and_exports_outs(tmp_path, monkeypatch, with_overrides):
    cfg = build_fixture_project(tmp_path)
    cfg.data["docker"] = dict(cfg.data["docker"])
    cfg.data["docker"]["cellranger_work_volume_prefix"] = "scpre-pipestance"
    cfg.data["docker"]["cellranger_volume_min_free_gb"] = 1
    override_path = tmp_path / "resources.json"
    if with_overrides:
        override_path.write_text('{"ALIGN_AND_COUNT": {"chunk.threads": 8}}')
        cfg.data["docker"]["cellranger_overrides"] = str(override_path)
    runner = DockerRunner(cfg)
    fastq_dir = tmp_path / "fastq"
    fastq_dir.mkdir()
    (fastq_dir / "GSM1_S1_L001_R1_001.fastq.gz").write_bytes(b"fq")
    calls = []
    ensured = []
    exports = []

    monkeypatch.setattr(runner, "_ensure_volume", ensured.append)
    monkeypatch.setattr(runner, "_volume_free_bytes",
                        lambda volume, image, platform: 10 * 1024 ** 3)
    monkeypatch.setattr(runner, "_volume_file_exists",
                        lambda volume, path, image, platform: False)

    def fake_run(image, platform, mounts, argv, name, logger, **kwargs):
        calls.append({"mounts": mounts, "argv": argv, "name": name})

    def fake_export(volume, source, destination_parent, image, platform,
                    logger):
        exports.append((volume, source, destination_parent))
        outs = destination_parent / "outs"
        outs.mkdir(parents=True)
        return outs

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_export_volume_tree", fake_export)

    outs = runner.cellranger_count(
        "GSM1", fastq_dir, tmp_path / "cellranger",
        tmp_path / "reference", 4, 16, True,
        __import__("logging").getLogger("test"),
    )

    assert ensured == ["scpre-pipestance-GSM1"]
    main = next(call for call in calls
                if call["argv"][0] == "/opt/cellranger/cellranger")
    assert ("scpre-pipestance-GSM1", "/work", False) in main["mounts"]
    assert ("--overrides=/resource-overrides.json" in main["argv"]) == with_overrides
    if with_overrides:
        assert (override_path, "/resource-overrides.json", True) in main["mounts"]
    assert exports == [("scpre-pipestance-GSM1", "/work/GSM1/outs",
                        tmp_path / "cellranger" / "GSM1")]
    assert outs == tmp_path / "cellranger" / "GSM1" / "outs"


def test_service_lock_rejects_second_scheduler(tmp_path):
    from scpre.__main__ import _acquire_service_lock
    from scpre.config import Config
    cfg = Config({}, tmp_path)
    first = _acquire_service_lock(cfg)
    try:
        with pytest.raises(SystemExit) as e:
            _acquire_service_lock(cfg)
        assert "already running" in str(e.value)
    finally:
        first.close()


def test_waiting_worker_obeys_pause_before_acquiring_slot(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    q.semaphores["converting"] = threading.Semaphore(0)
    worker = threading.Thread(target=q._worker, args=(sid,))
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and state.get_sample(sid)["status"] != "waiting":
        time.sleep(0.05)
    assert state.get_sample(sid)["status"] == "waiting"

    q.pause("test pause")
    worker.join(5)

    assert not worker.is_alive()
    assert state.get_sample(sid)["status"] == "queued"


def test_dashboard_pause_is_sticky_until_explicit_resume(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()

    q.pause("paused from dashboard")
    q.tick()

    assert q.paused_reason() == "paused from dashboard"
    assert not q.workers
    assert all(s["status"] == "queued" for s in state.list_samples())

    q.resume()
    q.tick()
    assert q.workers


def test_shutdown_persists_safe_state_when_docker_stop_fails(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.upsert_sample(sid, status="active", stage="converting")
    state.event_start(sid, "converting")
    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    q.workers[sid] = finished

    def unavailable(*args, **kwargs):
        raise RuntimeError("Docker daemon unavailable")

    q.runner.stop_sample_jobs = unavailable
    q.stop()

    assert state.get_sample(sid)["status"] == "queued"
    assert state.events_for(sid)[-1]["status"] == "interrupted"


def test_tool_exit_during_controlled_shutdown_is_not_a_failure(tmp_path):
    from scpre.docker_run import ToolError
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]

    def stopped_fasterq(*args, **kwargs):
        q._stop.set()
        raise ToolError("fasterq exited 143")

    q.runner.fasterq = stopped_fasterq
    q._worker(sid)

    assert state.get_sample(sid)["status"] == "queued"
    event = state.events_for(sid)[-1]
    assert event["stage"] == "converting"
    assert event["status"] == "interrupted"
    assert event["exit_code"] == 130


# --- pause on drive loss --------------------------------------------------

def test_queue_pauses_when_archive_drive_vanishes(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    shutil.rmtree(cfg.path("archive_root"))
    q.tick()
    assert q.paused_reason() and "archive" in q.paused_reason()
    # queued samples untouched
    assert all(s["status"] == "queued" for s in state.list_samples())
    # drive comes back -> auto-resume and complete
    cfg.path("archive_root").mkdir(parents=True)
    drain(q, state)
    assert q.paused_reason() is None
    assert all(s["status"] == "done" for s in state.list_samples())


def test_queue_pauses_when_docker_mount_view_is_stale(tmp_path):
    cfg, state, q = make_env(tmp_path)
    cfg.data["tools"]["mode"] = "docker"
    q.scan_inbox()
    q.runner.mounts_healthy = lambda _paths: "/host_mnt path is not a directory"

    q.tick()

    assert "Docker cannot access" in q.paused_reason()
    assert all(s["status"] == "queued" for s in state.list_samples())


# --- Docker Desktop bind-mount cleanup -----------------------------------

def _finish_with_deferred_cleanup(q, state, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        q.tick()
        samples = state.list_samples()
        if samples and all(s["status"] == "done" for s in samples) \
                and not q.workers:
            return samples
        time.sleep(0.05)
    raise AssertionError("samples did not reach deferred cleanup")


def test_deferred_cleanup_restarts_idle_desktop_once_for_batch(tmp_path):
    cfg, state, q = make_env(tmp_path)
    cfg.data["tools"]["mode"] = "docker"
    q.runner.defer_cleanup = True
    # Hold maintenance long enough to inspect the completed pending state.
    q.runner.external_containers = ["test-maintenance-hold"]
    q.scan_inbox()

    samples = _finish_with_deferred_cleanup(q, state)

    assert all(s["meta"].get("cleanup_pending") for s in samples)
    assert all((cfg.work_dir / s["id"] / "cellranger").exists()
               for s in samples)
    assert q.runner.desktop_restarts == 0

    q.runner.external_containers.clear()
    q.tick()

    assert q.runner.desktop_restarts == 1
    assert all(not (cfg.work_dir / s["id"]).exists() for s in samples)
    assert all(not state.get_sample(s["id"])["meta"].get("cleanup_pending")
               for s in samples)
    assert q.cleanup_state() == {"pending_samples": [], "status": None}


def test_deferred_cleanup_preserves_files_while_other_container_runs(tmp_path):
    cfg, state, q = make_env(tmp_path)
    cfg.data["tools"]["mode"] = "docker"
    q.runner.defer_cleanup = True
    q.runner.external_containers = ["unrelated-database"]
    q.scan_inbox()
    samples = _finish_with_deferred_cleanup(q, state)

    q.tick()

    assert q.runner.desktop_restarts == 0
    assert all((cfg.work_dir / s["id"]).exists() for s in samples)
    cleanup = q.cleanup_state()
    assert cleanup["pending_samples"]
    assert "unrelated-database" in cleanup["status"]

    q.runner.external_containers.clear()
    q.tick()

    assert q.runner.desktop_restarts == 1
    assert all(not (cfg.work_dir / s["id"]).exists() for s in samples)


def test_dashboard_exposes_deferred_cleanup_status(tmp_path):
    from fastapi.testclient import TestClient
    from scpre.server import create_app

    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    state.update_meta(sid, cleanup_pending=True,
                      cleanup_requested_at=time.time())
    state.kv_set("cleanup_status", "Waiting for Docker-idle cleanup")
    app = create_app(cfg, state, q, q.runner)

    body = TestClient(app).get("/api/state").json()

    assert body["cleanup"]["pending_samples"] == [sid]
    assert body["samples"][0]["meta"]["cleanup_pending"] is True
    assert any("Docker-idle cleanup" in w["message"]
               for w in body["warnings"])


def test_deferred_cleanup_never_deletes_before_done_checkpoint(tmp_path):
    cfg, state, q = make_env(tmp_path)
    cfg.data["tools"]["mode"] = "docker"
    q.runner.defer_cleanup = True
    q.scan_inbox()
    sid = state.list_samples()[0]["id"]
    marker = cfg.work_dir / sid / "cellranger" / sid / "keep-after-crash"
    marker.parent.mkdir(parents=True)
    marker.write_text("checkpoint")
    state.update_meta(sid, cleanup_pending=True,
                      cleanup_requested_at=time.time())
    state.upsert_sample(sid, status="queued", stage="cleaning")

    may_schedule = q._maintain_deferred_cleanup()

    assert may_schedule is True
    assert marker.read_text() == "checkpoint"
    assert q.runner.desktop_restarts == 0


# --- space preflight ------------------------------------------------------

def test_space_preflight_refuses(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    cfg.data["space"]["min_free_work_gb"] = 10 ** 9  # impossible reserve
    s = state.list_samples()[0]
    with pytest.raises(SpaceError) as e:
        preflight(s, cfg)
    assert "insufficient disk space" in str(e.value)


def test_space_preflight_rejects_filesystem_with_no_inodes(tmp_path,
                                                            monkeypatch):
    import scpre.space as space_mod
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    real_disk_usage = space_mod.disk_usage

    def no_inodes(path):
        info = real_disk_usage(path)
        if Path(path) == cfg.path("work_root"):
            info.update(inodes_total=1, inodes_free=0)
        return info

    monkeypatch.setattr(space_mod, "disk_usage", no_inodes)
    with pytest.raises(SpaceError) as e:
        preflight(state.list_samples()[0], cfg)
    assert "no free inodes" in str(e.value)


# --- archive verification catches corruption ------------------------------

def test_verify_catches_corrupted_archive(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    samples = drain(q, state)
    sid = samples[0]["id"]
    arch = cfg.archive_sample_dir(sid)
    zpath = arch / "archives" / f"{sid}.fastq.zip"
    # corrupt the archived zip, then force re-verification
    zpath.write_bytes(b"corrupted!")
    from scpre.stages import StageContext, stage_verifying
    from scpre.util import sample_logger
    ctx = StageContext(cfg, state, MockRunner(cfg), state.get_sample(sid),
                       sample_logger(sid, cfg.logs_dir / f"{sid}.log"))
    with pytest.raises(Exception) as e:
        stage_verifying(ctx)
    assert "verification failed" in str(e.value)


# --- repro script ---------------------------------------------------------

def test_repro_script_contents(tmp_path):
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    samples = drain(q, state)
    sid = samples[0]["id"]
    arch = cfg.archive_sample_dir(sid)
    script = (arch / "repro" / f"reproduce_{sid}.sh").read_text()
    versions = json.loads((arch / "repro" / "versions.json").read_text())
    assert "cellranger count" in script
    assert "velocyto run" in script
    assert sid in script
    assert versions["srr_to_sample"]
    assert versions["tool_versions"]["cellranger"]
    assert versions["reference_dir"]
    assert "repo_commit" in versions


def test_velocyto_gets_writable_outs_and_removes_sort_scratch(tmp_path):
    from scpre.docker_run import DockerRunner

    cfg = build_fixture_project(tmp_path)
    runner = DockerRunner(cfg)
    outs = tmp_path / "outs"
    matrix = outs / "filtered_feature_bc_matrix"
    matrix.mkdir(parents=True)
    (outs / "possorted_genome_bam.bam").write_bytes(b"bam")
    (matrix / "barcodes.tsv.gz").write_bytes(b"barcodes")
    gtf = tmp_path / "genes" / "genes.gtf"
    gtf.parent.mkdir()
    gtf.write_text("gtf")
    loom_dir = tmp_path / "loom"

    def fake_run(_image, _platform, mounts, _argv, _name, _logger, **_kwargs):
        outs_mount = [m for m in mounts if m[1] == "/outs"][0]
        assert outs_mount == (outs, "/outs", False)
        (outs / "cellsorted_possorted_genome_bam.bam").write_bytes(b"scratch")
        loom_dir.mkdir(parents=True, exist_ok=True)
        (loom_dir / "sample.loom").write_bytes(b"loom")

    runner._run = fake_run
    runner.velocyto("sample", outs, gtf, loom_dir, 2,
                     __import__("logging").getLogger("test"))

    assert (loom_dir / "sample.loom").is_file()
    assert not list(outs.glob("cellsorted_possorted_genome_bam.bam*"))


def test_velocyto_reads_cellranger_volume_on_windows(tmp_path, monkeypatch):
    cfg = build_fixture_project(tmp_path)
    cfg.data["docker"] = dict(cfg.data["docker"])
    cfg.data["docker"]["cellranger_work_volume_prefix"] = "scpre-pipestance"
    runner = DockerRunner(cfg)
    outs = tmp_path / "outs"
    matrix = outs / "filtered_feature_bc_matrix"
    matrix.mkdir(parents=True)
    (outs / "possorted_genome_bam.bam").write_bytes(b"bam")
    (matrix / "barcodes.tsv.gz").write_bytes(b"barcodes")
    gtf = tmp_path / "genes" / "genes.gtf"
    gtf.parent.mkdir()
    gtf.write_text("gtf")
    loom_dir = tmp_path / "loom"
    calls = []
    monkeypatch.setattr(runner, "_volume_exists", lambda name: True)

    def fake_run(_image, _platform, mounts, argv, name, _logger, **_kwargs):
        calls.append((mounts, argv, name))
        if name.endswith("-velocyto"):
            loom_dir.mkdir(parents=True, exist_ok=True)
            (loom_dir / "sample.loom").write_bytes(b"loom")

    monkeypatch.setattr(runner, "_run", fake_run)

    runner.velocyto("sample", outs, gtf, loom_dir, 2,
                    __import__("logging").getLogger("test"))

    mounts, argv, _name = calls[0]
    assert ("scpre-pipestance-sample", "/cellranger", False) in mounts
    assert "/cellranger/sample/outs/possorted_genome_bam.bam" in argv
    assert calls[-1][2].endswith("-velocyto-cleanup")


# --- dashboard API --------------------------------------------------------

def test_api_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from scpre.server import create_app
    cfg, state, q = make_env(tmp_path)
    q.scan_inbox()
    drain(q, state)
    app = create_app(cfg, state, q, q.runner)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/api/state").json()
    assert r["project"] == "dryrun"
    assert len(r["samples"]) == 2
    s0 = r["samples"][0]
    assert s0["status"] == "done"
    assert s0["links"]["web_summary"]
    assert any(ev["stage"] == "cellranger" and ev["status"] == "completed"
               for ev in s0["stage_events"])

    assert client.get("/api/disk").json()["work"]["ok"]
    sid = s0["id"]
    r = client.get(f"/api/logs/{sid}?tail=50").json()
    assert r["lines"]
    assert client.get(f"/files/web_summary/{sid}").status_code == 200
    assert client.get(f"/files/loom/{sid}").status_code == 200
    assert client.get(f"/files/outs/{sid}").json()["files"]
    # path traversal is blocked
    assert client.get(f"/files/outs/{sid}/get?path=../../etc/passwd").status_code == 400
    assert client.get("/api/logs/..%2Fetc").status_code in (400, 404)
    # retry refused for done samples
    assert client.post(f"/api/retry/{sid}").status_code == 409
    # pause/resume roundtrip
    assert client.post("/api/pause").json()["ok"]
    assert client.get("/api/state").json()["paused"]
    assert client.post("/api/resume").json()["ok"]
    # index served
    assert "scpre" in client.get("/").text


def test_dashboard_warns_when_cellranger_exceeds_docker_cpus(tmp_path):
    from fastapi.testclient import TestClient
    from scpre.server import create_app
    cfg, state, q = make_env(tmp_path)
    cfg.data["concurrency"]["cellranger_cores"] = 12
    q.runner.resource_info = lambda: {"memory_total": None, "cpus": 8}
    app = create_app(cfg, state, q, q.runner)

    warnings = TestClient(app).get("/api/state").json()["warnings"]

    assert any("12 cores" in w["message"] and "8 CPUs" in w["message"]
               for w in warnings)


# --- FASTQ mode -----------------------------------------------------------

def test_fastq_mode_sample(tmp_path):
    import gzip as _gzip
    cfg, state, q = make_env(tmp_path)
    fq_src = tmp_path / "user_fastqs"
    fq_src.mkdir()
    def wr(name, read_len):
        with _gzip.open(fq_src / name, "wt") as f:
            for i in range(50):
                f.write(f"@r{i}\n{'A'*read_len}\n+\n{'F'*read_len}\n")
    wr("mysamp_L001_R1.fastq.gz", 28)
    wr("mysamp_L001_R2.fastq.gz", 90)
    q.add_fastq_sample("MySample", sorted(fq_src.glob("*.fastq.gz")))
    samples = drain(q, state)
    s = [x for x in samples if x["id"] == "MySample"][0]
    assert s["status"] == "done"
    arch = cfg.archive_sample_dir("MySample")
    assert (arch / "archives" / "MySample.fastq.zip").is_file()
    assert (arch / "loom" / "MySample.loom").is_file()


# --- read-role detection (equal-length R1/R2, poly-dT signal) --------------

def _write_fq(path, n, mk_seq):
    with open(path, "w") as f:
        for i in range(n):
            s = mk_seq(i)
            f.write(f"@r{i}\n{s}\n+\n{'F' * len(s)}\n")


def test_assign_roles_equal_length_polyT(tmp_path):
    import random
    from scpre.stages import _assign_roles
    from scpre.util import app_logger
    rng = random.Random(7)
    rand = lambda n: "".join(rng.choice("ACGT") for _ in range(n))
    r1 = tmp_path / "SRRX_1.fastq"   # 101bp: CB+UMI then poly-T
    r2 = tmp_path / "SRRX_2.fastq"   # 101bp: cDNA
    _write_fq(r1, 200, lambda i: rand(28) + "T" * 60 + rand(13))
    _write_fq(r2, 200, lambda i: rand(101))
    roles = _assign_roles([r1, r2], app_logger())
    assert roles[r1] == "R1" and roles[r2] == "R2"


def test_assign_roles_with_index_read(tmp_path):
    import random
    from scpre.stages import _assign_roles
    from scpre.util import app_logger
    rng = random.Random(8)
    rand = lambda n: "".join(rng.choice("ACGT") for _ in range(n))
    i1 = tmp_path / "SRRX_1.fastq"
    r1 = tmp_path / "SRRX_2.fastq"
    r2 = tmp_path / "SRRX_3.fastq"
    _write_fq(i1, 200, lambda i: rand(8))
    _write_fq(r1, 200, lambda i: rand(28) + "T" * 60)
    _write_fq(r2, 200, lambda i: rand(90))
    roles = _assign_roles([i1, r1, r2], app_logger())
    assert roles == {i1: "I1", r1: "R1", r2: "R2"}


def test_assign_roles_ambiguous_raises(tmp_path):
    import random
    import pytest as _pytest
    from scpre.docker_run import ToolError
    from scpre.stages import _assign_roles
    from scpre.util import app_logger
    rng = random.Random(9)
    rand = lambda n: "".join(rng.choice("ACGT") for _ in range(n))
    a = tmp_path / "SRRX_1.fastq"
    b = tmp_path / "SRRX_2.fastq"
    _write_fq(a, 200, lambda i: rand(101))
    _write_fq(b, 200, lambda i: rand(101))
    with _pytest.raises(ToolError) as e:
        _assign_roles([a, b], app_logger())
    assert "read_maps" in str(e.value)


def test_completed_raw_manifest_is_reusable_and_size_checked(tmp_path):
    from scpre.stages import _validated_raw_manifest, _write_raw_manifest
    raw = tmp_path / "raw"
    raw.mkdir()
    f1 = raw / "SRR1_1.fastq"
    f2 = raw / "SRR1_2.fastq"
    f1.write_text("@r\nAAAA\n+\nFFFF\n")
    f2.write_text("@r\nCCCC\n+\nFFFF\n")
    _write_raw_manifest(raw, "SRR1", [f1, f2], 1)

    files, spots = _validated_raw_manifest(raw, "SRR1")
    assert files == [f1, f2]
    assert spots == 1

    f1.write_text(f1.read_text() + "corrupt")
    assert _validated_raw_manifest(raw, "SRR1") is None
