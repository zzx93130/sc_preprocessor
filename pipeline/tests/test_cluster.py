import json
import sqlite3
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scpre.artifact_store import ArtifactCache, write_catalog
from scpre.cluster_archive import ArchiveCommitter, ArchiveError, build_bundle
from scpre.cluster_manifest import ManifestError, load_verified, write_manifest
from scpre.config import Config
from scpre.executors.base import JobStatus
from scpre.executors.pbs import ExecutorError, PBSExecutor
from scpre.manager import ClusterManager
from scpre.migration import build_plan
from scpre.scratch import write_json_atomic
from scpre.state import State


def manifest_document(tmp_path):
    source = tmp_path / "SRR1.sra"
    source.write_bytes(b"sra-data")
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "attempt_id": "a1",
        "sample": "GSM1",
        "project": "default",
        "inputs": [{
            "srr": "SRR1", "path": str(source),
            "size": source.stat().st_size, "sha256": digest,
        }],
        "paths": {
            "shared_root": str(tmp_path / "shared"),
            "scratch_root": str(tmp_path / "scratch"),
            "archive_root": str(tmp_path / "archive"),
        },
        "resources": {"cores": 4, "memory_gb": 16},
        "artifacts": {},
        "pipeline": {},
    }


def test_manifest_is_immutable_and_tamper_evident(tmp_path):
    path = tmp_path / "manifest.json"
    document = manifest_document(tmp_path)
    digest = write_manifest(path, document)
    loaded, actual = load_verified(path, digest)
    assert loaded["sample"] == "GSM1"
    assert actual == digest
    path.chmod(0o644)
    path.write_text(path.read_text().replace("GSM1", "GSM2"))
    with pytest.raises(ManifestError, match="checksum mismatch"):
        load_verified(path, digest)


def test_state_enforces_one_active_attempt_per_sample(tmp_path):
    state = State(tmp_path / "state.db")
    state.upsert_sample("GSM1")
    state.create_attempt("GSM1", "a1", status="submitted")
    with pytest.raises(sqlite3.IntegrityError):
        state.create_attempt("GSM1", "a2", status="preparing")
    state.update_attempt("a1", status="failed")
    state.create_attempt("GSM1", "a2", status="preparing")
    assert state.active_attempt_for("GSM1")["attempt_id"] == "a2"


def test_artifact_cache_reuses_verified_version(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool").write_text("v1")
    catalog = write_catalog(
        tmp_path / "catalog.json", {"cellranger": source}
    )
    spec = catalog["artifacts"]["cellranger"]
    cache = ArtifactCache(tmp_path / "cache")
    first = cache.materialize(spec)
    second = cache.materialize(spec)
    assert first == second
    assert (first / "tool").read_text() == "v1"


def test_two_phase_archive_commit_and_idempotence(tmp_path):
    fastq = tmp_path / "work" / "fastq"
    outs = tmp_path / "work" / "outs"
    fastq.mkdir(parents=True)
    outs.mkdir(parents=True)
    (fastq / "GSM1_R1.fastq.gz").write_bytes(b"fq")
    (outs / "web_summary.html").write_text("ok")
    (outs / "metrics_summary.csv").write_text("metric\n1\n")
    bundle = tmp_path / "GSM1.tar.zst"
    build_bundle(
        bundle, {"fastq": fastq, "results/outs": outs}, "zstd"
    )
    committer = ArchiveCommitter(
        tmp_path / "archive" / "samples",
        tmp_path / "archive" / ".staging",
    )
    final = committer.commit(
        "GSM1", "default", "a1", "f" * 64, bundle, "zstd",
        {
            "results/outs/web_summary.html":
                outs / "web_summary.html",
            "results/outs/metrics_summary.csv":
                outs / "metrics_summary.csv",
        },
    )
    record = json.loads((final / "COMMITTED.json").read_text())
    assert record["attempt_id"] == "a1"
    assert (final / "archive.tar.zst.sha256").is_file()
    assert committer.commit(
        "GSM1", "default", "a1", "f" * 64, bundle, "zstd", {}
    ) == final
    with pytest.raises(ArchiveError, match="already has"):
        committer.commit(
            "GSM1", "default", "a2", "e" * 64, bundle, "zstd", {}
        )


def test_pbs_executor_submits_static_script_and_parses_status(tmp_path):
    class FakePBS(PBSExecutor):
        def __init__(self):
            super().__init__({
                "qsub": "qsub", "qstat": "qstat", "qdel": "qdel",
                "worker_script": "/opt/scpre/scpre-worker.pbs",
                "select": "select=1:ncpus=8:mem=32gb",
                "walltime": "12:00:00", "scratch_resource": "scratch",
            })
            self.commands = []

        def _call(self, command):
            self.commands.append(command)
            if command[0] == "qsub":
                return subprocess.CompletedProcess(
                    command, 0, "123.server\n", ""
                )
            return subprocess.CompletedProcess(
                command, 0,
                json.dumps({"Jobs": {"123.server": {
                    "job_state": "R", "exec_host": "compute01/0",
                }}}), "",
            )

    executor = FakePBS()
    job_id = executor.submit(
        PurePosixPath("/vol-rep1/scpre/control/m.json"), "a" * 64,
        preferred_node="compute01", scratch_bytes=25 * 1024 ** 3,
    )
    assert job_id == "123.server"
    command = executor.commands[0]
    assert "/opt/scpre/scpre-worker.pbs" == command[-1]
    assert "scratch=25gb" in command
    status = executor.status(job_id)
    assert status.state == "running"
    assert status.node == "compute01"
    with pytest.raises(ExecutorError):
        executor.submit(
            PurePosixPath("/tmp/a,b"), "a" * 64
        )


class FakeExecutor:
    def __init__(self):
        self.jobs = {}
        self.cancelled = []

    def healthy(self):
        return True

    def submit(self, manifest_path, manifest_sha256,
               preferred_node="", scratch_bytes=0):
        job_id = f"{len(self.jobs) + 1}.server"
        self.jobs[job_id] = JobStatus(
            job_id, "queued", preferred_node, None, "Q"
        )
        return job_id

    def status(self, job_id):
        return self.jobs.get(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.jobs.pop(job_id, None)


def cluster_env(tmp_path):
    mount = tmp_path / "vol-rep1"
    shared = mount / "scpre"
    scratch = tmp_path / "ssd"
    mount.mkdir()
    scratch.mkdir()
    (mount / ".scpre-volume-id").write_text("SCPRE_GLUSTER")
    (scratch / ".scpre-volume-id").write_text("SCPRE_SCRATCH")
    artifact_sources = {}
    for name in ("cellranger", "reference"):
        path = shared / "artifacts" / name
        path.mkdir(parents=True)
        (path / "content").write_text(name)
        artifact_sources[name] = path
    for name in ("sratools", "crbase", "velocyto"):
        path = shared / "artifacts" / f"{name}.sif"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        artifact_sources[name] = path
    catalog = shared / "artifacts" / "catalog.json"
    write_catalog(catalog, artifact_sources)
    ready = shared / "incoming" / "ready" / "SRR1"
    ready.mkdir(parents=True)
    (ready / "SRR1.sra").write_bytes(b"input")
    table = shared / "incoming" / "SraRunTable.csv"
    table.write_text(
        "Run,Sample Name,GEO_Accession (exp),Organism\n"
        "SRR1,GSM1,GSM1,Homo sapiens\n"
    )
    data = {
        "project": "default",
        "paths": {
            "state_db": str(tmp_path / "state" / "state.sqlite"),
            "logs_dir": str(tmp_path / "logs"),
            "work_root": str(tmp_path / "manager"),
            "archive_root": str(shared / "archive" / "samples"),
            "inbox_dir": str(shared / "incoming" / "ready"),
            "cellranger_dir": str(artifact_sources["cellranger"]),
            "reference_dir": str(artifact_sources["reference"]),
        },
        "concurrency": {
            "cellranger_cores": 4, "cellranger_mem_gb": 16,
            "convert_threads": 2, "max_active_samples": 2,
            "max_cellranger_jobs": 1,
        },
        "space": {
            "fastq_multiplier": 2.2, "conversion_multiplier": 10,
            "cellranger_multiplier": 2.5, "min_free_work_gb": 0,
            "min_free_archive_gb": 0,
        },
        "metadata": {
            "sra_run_table": str(table), "process_all": False,
            "read_maps": {"SRR1": "1=R1,2=R2"},
        },
        "tools": {"mode": "apptainer"},
        "cluster": {
            "enabled": True,
            # Unit tests run on small temporary volumes.  Production retains
            # the 20 GiB manager reserve from the default/example config.
            "manager_min_free_gb": 0,
            "shared_mount": str(mount),
            "shared_root": str(shared),
            "scratch_mount": str(scratch),
            "scratch_root": str(scratch / "scpre"),
            "shared_identity": "SCPRE_GLUSTER",
            "scratch_identity": "SCPRE_SCRATCH",
            "require_mountpoints": False,
            "artifacts_catalog": str(catalog),
            "max_running_jobs": 2,
            "download_enabled": False,
            "archive_compression": "gz",
            "pbs": {
                "worker_script": "/opt/scpre/scpre-worker.pbs",
                "select": "select=1:ncpus=4:mem=20gb",
            },
        },
    }
    cfg = Config(data, tmp_path)
    state = State(cfg.state_db)
    executor = FakeExecutor()
    return cfg, state, ClusterManager(cfg, state, executor), shared


def test_manager_dispatch_and_commit_reconciliation(tmp_path):
    cfg, state, manager, shared = cluster_env(tmp_path)
    assert manager.scan_inbox() == 1
    manager.tick()
    sample = state.get_sample("GSM1")
    assert sample["status"] == "active"
    attempt = state.active_attempt_for("GSM1")
    assert attempt["pbs_job_id"] == "1.server"
    final = cfg.archive_sample_dir("GSM1")
    final.mkdir(parents=True)
    committed = {
        "attempt_id": attempt["attempt_id"],
        "manifest_sha256": attempt["manifest_sha256"],
    }
    (final / "COMMITTED.json").write_text(json.dumps(committed))
    write_json_atomic(
        shared / "control" / "events"
        / f"{attempt['attempt_id']}.json",
        {
            "attempt_id": attempt["attempt_id"], "sample": "GSM1",
            "status": "completed", "exit_code": 0, "node": "compute01",
            "manifest_sha256": attempt["manifest_sha256"],
        },
    )
    manager.reconcile()
    sample = state.get_sample("GSM1")
    assert sample["status"] == "done"
    assert sample["meta"]["compute_node"] == "compute01"
    assert state.get_attempt(attempt["attempt_id"])["status"] == "completed"
    assert list(
        (shared / "control" / "state-backups").glob("state-*.sqlite")
    )


def test_manager_failed_worker_preserves_same_node_affinity(tmp_path):
    _cfg, state, manager, shared = cluster_env(tmp_path)
    manager.scan_inbox()
    manager.tick()
    attempt = state.active_attempt_for("GSM1")
    write_json_atomic(
        shared / "control" / "events"
        / f"{attempt['attempt_id']}.json",
        {
            "attempt_id": attempt["attempt_id"], "sample": "GSM1",
            "status": "failed", "exit_code": 1, "node": "compute02",
            "stage": "cellranger", "scratch_path": "/ssd/scpre/jobs/GSM1/a",
            "manifest_sha256": attempt["manifest_sha256"],
            "error": "test failure",
        },
    )
    manager.reconcile()
    assert state.get_sample("GSM1")["status"] == "failed"
    assert manager.retry("GSM1") == ""
    sample = state.get_sample("GSM1")
    assert sample["status"] == "queued"
    assert sample["meta"]["retry_affinity"] == "compute02"


def test_reconcile_pauses_without_failing_jobs_when_gluster_is_unavailable(
        tmp_path):
    cfg, state, manager, _shared = cluster_env(tmp_path)
    manager.scan_inbox()
    manager.tick()
    attempt = state.active_attempt_for("GSM1")
    assert attempt is not None

    (Path(cfg.data["cluster"]["shared_mount"])
     / ".scpre-volume-id").unlink()
    manager.reconcile()

    assert state.get_sample("GSM1")["status"] == "active"
    assert state.get_attempt(attempt["attempt_id"])["status"] == "submitted"
    assert manager.paused_reason()


def test_cluster_dashboard_exposes_pbs_state(tmp_path):
    from fastapi.testclient import TestClient
    from scpre.server import create_app

    cfg, state, manager, _shared = cluster_env(tmp_path)
    manager.scan_inbox()
    manager.tick()
    body = TestClient(
        create_app(cfg, state, manager, manager)
    ).get("/api/state").json()
    assert body["cluster"]["mode"] == "pbs"
    assert body["samples"][0]["meta"]["pbs_job_id"] == "1.server"
    assert "incoming" in body["disks"]


def test_migration_plan_is_read_only_and_requeues_interrupted(tmp_path):
    source = State(tmp_path / "old" / "state.db")
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    (inbox / "SRR1").write_bytes(b"sra")
    source.upsert_sample(
        "GSM1", project="default", mode="sra", srrs=["SRR1"],
        status="active", stage="cellranger", meta={},
    )
    output = tmp_path / "plan.json"
    plan = build_plan(source.db_path, archive, inbox, output)
    assert plan["summary"]["requeue"] == 1
    assert plan["summary"]["requeue_with_all_inputs"] == 1
    assert plan["samples"][0]["source_status"] == "active"
    assert source.get_sample("GSM1")["status"] == "active"


def test_cluster_fastq_ingest_is_checksummed_and_manifested(tmp_path):
    _cfg, state, manager, _shared = cluster_env(tmp_path)
    source = tmp_path / "SampleA_L001_R1_001.fastq.gz"
    source.write_bytes(b"\x1f\x8bfastq")
    manager.add_fastq_sample("SampleA", [source])
    sample = state.get_sample("SampleA")
    assert sample["mode"] == "fastq"
    record = sample["meta"]["input_records"][0]
    assert Path(record["path"]).is_file()
    manager.dispatch(sample)
    attempt = state.active_attempt_for("SampleA")
    manifest, _ = load_verified(
        Path(attempt["manifest_path"]), attempt["manifest_sha256"]
    )
    assert manifest["inputs"][0]["kind"] == "fastq"
