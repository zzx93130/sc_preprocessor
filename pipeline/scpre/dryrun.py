"""End-to-end dry-run on generated fixtures: exercises the full state machine
(SRA table grouping, conversion naming, cellranger/velocyto contract, archive,
checksum verification, cleanup) with mock tools — no network, no Docker.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml

from .config import Config
from .docker_run import MockRunner
from .queue import Queue
from .state import State

FIXTURE_TABLE = """Run,Sample Name,GEO_Accession (exp),Organism,Instrument,LibraryLayout,LibraryStrategy
SRR900001,GSM900001,GSM900001,Homo sapiens,NovaSeq X,PAIRED,RNA-Seq
SRR900002,GSM900001,GSM900001,Homo sapiens,NovaSeq X,PAIRED,RNA-Seq
SRR900003,GSM900002,GSM900002,Homo sapiens,NovaSeq X,PAIRED,RNA-Seq
"""


def build_fixture_project(base: Path) -> Config:
    """Create a throwaway project layout with mock tools + inputs."""
    (base / "runs").mkdir(parents=True, exist_ok=True)
    cr = base / "fake_tools" / "cellranger-10.1.0"
    cr.mkdir(parents=True, exist_ok=True)
    (cr / "cellranger").write_text("#!/bin/sh\necho mock\n")
    (cr / "cellranger").chmod(0o755)
    (cr / ".version").write_text("10.1.0-mock")
    ref = base / "fake_tools" / "refdata-mock"
    (ref / "genes").mkdir(parents=True, exist_ok=True)
    (ref / "reference.json").write_text('{"genomes": ["mock"], "version": "mock"}')
    (ref / "genes" / "genes.gtf").write_text(
        "chr1\tmock\tgene\t1\t1000\t.\t+\t.\tgene_id \"MOCK1\";\n")
    work = base / "work_ssd"
    archive = base / "archive_drive"
    inbox = base / "inbox"
    for d in (work, archive, inbox):
        d.mkdir(parents=True, exist_ok=True)
    (inbox / "SraRunTable.csv").write_text(FIXTURE_TABLE)
    for srr in ("SRR900001", "SRR900002", "SRR900003"):
        (inbox / srr).write_bytes(b"NCBI-mock-sra" + os.urandom(512))

    data = {
        "project": "dryrun",
        "paths": {
            "cellranger_dir": str(cr),
            "reference_dir": str(ref),
            "work_root": str(work),
            "archive_root": str(archive),
            "inbox_dir": str(inbox),
        },
        "concurrency": {"max_active_samples": 2, "max_cellranger_jobs": 1,
                        "cellranger_cores": 2, "cellranger_mem_gb": 4,
                        "convert_threads": 2},
        "space": {"min_free_work_gb": 0.001, "min_free_archive_gb": 0.001,
                  "fastq_multiplier": 2.0, "cellranger_multiplier": 2.0},
        "tools": {"mode": "mock"},
        "cleanup": {"remove_inbox": False},
    }
    cfg = Config(data, base)
    cfg.save()
    return cfg


def run_dry_run(keep: bool = False, base_dir: str = None,
                fail_stage: str = None) -> int:
    base = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="scpre_dryrun_"))
    print(f"dry-run project: {base}")
    cfg = build_fixture_project(base)
    state = State(cfg.state_db)
    runner = MockRunner(cfg, fail_stage=fail_stage)
    q = Queue(cfg, state, runner=runner)

    n = q.scan_inbox()
    print(f"registered {n} sample(s) from fixture SraRunTable")

    deadline = time.time() + 120
    while time.time() < deadline:
        q.tick()
        samples = state.list_samples()
        if samples and all(s["status"] in ("done", "failed") for s in samples) \
                and not q.workers:
            break
        time.sleep(0.2)

    ok = True
    print()
    for s in state.list_samples():
        line = f"  {s['id']}: {s['status']}"
        if s["status"] == "done":
            arch = cfg.archive_sample_dir(s["id"])
            checks = {
                "fastq.zip": (arch / "archives" / f"{s['id']}.fastq.zip").is_file(),
                "zip.sha256": (arch / "archives" / f"{s['id']}.fastq.zip.sha256").is_file(),
                "web_summary": (arch / "results" / "outs" / "web_summary.html").is_file(),
                "matrix": (arch / "results" / "outs" / "filtered_feature_bc_matrix"
                           / "matrix.mtx.gz").is_file(),
                "loom": (arch / "loom" / f"{s['id']}.loom").is_file(),
                "repro": (arch / "repro" / f"reproduce_{s['id']}.sh").is_file(),
                "report": (cfg.reports_dir / f"{s['id']}.json").is_file(),
                "work cleaned": not any((cfg.work_dir / s["id"] / d).exists()
                                        for d in ("fastq", "cellranger", "loom")),
            }
            bad = [k for k, v in checks.items() if not v]
            if bad:
                ok = False
                line += f"  MISSING: {', '.join(bad)}"
            else:
                line += "  all outputs archived+verified, intermediates cleaned"
        elif not fail_stage:
            ok = False
            line += f"  ({s['stage']}: {s['error']})"
        print(line)

    if fail_stage:  # failure-injection mode is used by tests
        return 0 if any(s["status"] == "failed" for s in state.list_samples()) else 1
    print()
    if ok and state.list_samples():
        print("DRY-RUN PASSED — full pipeline validated with mock tools.")
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
        return 0
    print("DRY-RUN FAILED — inspect logs under", cfg.logs_dir)
    return 1
