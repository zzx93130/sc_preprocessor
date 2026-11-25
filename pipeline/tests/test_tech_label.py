"""Per-sample technology label: rna / snrna / atac / multiome."""
import json
import time

from scpre import sra_table, tech
from scpre.docker_run import MockRunner
from scpre.dryrun import build_fixture_project
from scpre.queue import Queue
from scpre.state import State

TABLE = """Run,Sample Name,GEO_Accession (exp),Organism,Assay Type,tech,multiome_sample
SRR1,GSM1,GSM1,Homo sapiens,RNA-Seq,,
SRR2,GSM2,GSM2,Homo sapiens,RNA-Seq,snRNA,
SRR3,GSM3,GSM3,Homo sapiens,ATAC-seq,,
SRR4,GSM3,GSM3,Homo sapiens,ATAC-seq,,
SRR5,GSM5,GSM5,Homo sapiens,RNA-Seq,,donorA
SRR6,GSM6,GSM6,Homo sapiens,ATAC-seq,,donorA
"""
LAYOUTS = {"SRR3": [50, 16, 50], "SRR4": [8, 50, 16, 50], "SRR6": [50, 24, 49]}
RUNS = ("SRR1", "SRR2", "SRR3", "SRR4", "SRR5", "SRR6")


def project(tmp_path, table=TABLE, runs=RUNS, atac_tools=True, **metadata):
    cfg = build_fixture_project(tmp_path)
    inbox = cfg.path("inbox_dir")
    for old in inbox.iterdir():
        old.unlink()
    (inbox / "SraRunTable.csv").write_text(table)
    for run in runs:
        (inbox / run).write_bytes(b"NCBI-mock-sra" + run.encode())
    if atac_tools:
        tools = tmp_path / "fake_tools"
        for key, name in (("cellranger_atac_dir", "cellranger-atac"),
                          ("cellranger_arc_dir", "cellranger-arc")):
            d = tools / f"{name}-2.2.0"
            d.mkdir(parents=True)
            (d / name).write_text("#!/bin/sh\n")
            (d / ".version").write_text(f"{name}-2.2.0-mock")
            cfg.data["paths"][key] = str(d)
        ref = tools / "refdata-cellranger-arc-mock"
        (ref / "fasta").mkdir(parents=True)
        (ref / "reference.json").write_text("{}")
        cfg.data["paths"]["atac_reference_dir"] = str(ref)
    # fresh dict: Config shares nested defaults between instances
    cfg.data["metadata"] = {**cfg.data["metadata"], **metadata}
    state = State(cfg.state_db)
    runner = MockRunner(cfg)
    runner.read_layouts = dict(LAYOUTS)
    return cfg, state, runner, Queue(cfg, state, runner=runner)


def drain(queue, state, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        queue.tick()
        samples = state.list_samples()
        if samples and not queue.workers and \
                all(s["status"] in ("done", "failed") for s in samples):
            return {s["id"]: s for s in samples}
        time.sleep(0.1)
    raise AssertionError("queue did not drain")


def test_labels_from_table_assay_and_pairing(tmp_path):
    cfg, state, _runner, queue = project(tmp_path)
    assert queue.scan_inbox() == 4
    got = {s["id"]: s for s in state.list_samples()}
    assert {k: v["meta"]["tech"] for k, v in got.items()} == {
        "GSM1": "rna", "GSM2": "snrna", "GSM3": "atac", "donorA": "multiome"}
    assert got["donorA"]["srrs"] == ["SRR5", "SRR6"]
    assert got["donorA"]["meta"]["library_types"] == {"SRR5": "gex", "SRR6": "atac"}
    assert got["donorA"]["meta"]["multiome_members"] == ["GSM5", "GSM6"]
    assert "GSM5" not in got and "GSM6" not in got


def test_config_label_and_pairs_override_table(tmp_path):
    table = TABLE.replace(",donorA", ",")
    cfg, state, _runner, queue = project(
        tmp_path, table=table, tech={"GSM1": "snRNA-seq"},
        multiome_pairs={"pairB": ["GSM5", "GSM6"]})
    queue.scan_inbox()
    got = {s["id"]: s["meta"]["tech"] for s in state.list_samples()}
    assert got["GSM1"] == "snrna" and got["pairB"] == "multiome"


def test_all_techs_run_to_verified_archive(tmp_path):
    cfg, state, runner, queue = project(tmp_path)
    queue.scan_inbox()
    done = drain(queue, state)
    assert {k: v["status"] for k, v in done.items()} == dict.fromkeys(
        ("GSM1", "GSM2", "GSM3", "donorA"), "done"), \
        {k: v["error"] for k, v in done.items()}
    tools = {c[1]: c for c in runner.calls}
    assert tools["GSM1"][0] == "cellranger" and tools["GSM1"][3] == ()
    assert tools["GSM2"][3] == ("--include-introns=true",)
    assert tools["GSM3"][0] == "cellranger-atac"
    assert tools["GSM3"][2] == cfg.data["paths"]["atac_reference_dir"]
    assert tools["donorA"][0] == "cellranger-arc"
    assert tools["donorA"][3:5] == ("donorA_gex", "donorA_atac")

    arch = cfg.archive_sample_dir("GSM3")
    outs = arch / "results" / "outs"
    assert (outs / "fragments.tsv.gz.tbi").is_file()
    assert not (outs / "possorted_bam.bam").exists()      # BAM kept out by default
    assert not (arch / "loom").exists()
    names = {f["name"] for f in json.loads(
        (arch / "archives" / "GSM3.fastq.manifest.json").read_text())["files"]}
    assert names == {
        "GSM3_S1_L001_R1_001.fastq.gz", "GSM3_S1_L001_R2_001.fastq.gz",
        "GSM3_S1_L001_R3_001.fastq.gz", "GSM3_S1_L002_I1_001.fastq.gz",
        "GSM3_S1_L002_R1_001.fastq.gz", "GSM3_S1_L002_R2_001.fastq.gz",
        "GSM3_S1_L002_R3_001.fastq.gz"}
    checks = {c["name"]: c["ok"] for c in json.loads(
        (cfg.reports_dir / "GSM3.json").read_text())["checks"]}
    assert checks["fragments_sha256"] and checks["archive:results/outs/fragments.tsv.gz.tbi"]
    script = (arch / "repro" / "reproduce_GSM3.sh").read_text()
    assert "/opt/cellranger/cellranger-atac count" in script and "velocyto run" not in script

    arch = cfg.archive_sample_dir("donorA")
    names = {f["name"] for f in json.loads(
        (arch / "archives" / "donorA.fastq.manifest.json").read_text())["files"]}
    assert names == {
        "donorA_gex_S1_L001_R1_001.fastq.gz", "donorA_gex_S1_L001_R2_001.fastq.gz",
        "donorA_atac_S1_L002_R1_001.fastq.gz", "donorA_atac_S1_L002_R2_001.fastq.gz",
        "donorA_atac_S1_L002_R3_001.fastq.gz"}
    assert (arch / "results" / "outs" / "atac_fragments.tsv.gz").is_file()
    script = (arch / "repro" / "reproduce_donorA.sh").read_text()
    assert "/fastq,donorA_atac,Chromatin Accessibility" in script
    assert "--create-bam=false" in script

    # rna keeps its loom and its original reproduction command
    assert (cfg.archive_sample_dir("GSM1") / "loom" / "GSM1.loom").is_file()
    assert "--include-introns" not in (
        cfg.archive_sample_dir("GSM1") / "repro" / "reproduce_GSM1.sh").read_text()
    assert "--include-introns=true" in (
        cfg.archive_sample_dir("GSM2") / "repro" / "reproduce_GSM2.sh").read_text()


def test_atac_without_barcode_read_fails_clearly(tmp_path):
    table = "Run,Sample Name,Assay Type\nSRR3,GSM3,ATAC-seq\n"
    cfg, state, runner, queue = project(tmp_path, table=table, runs=("SRR3",))
    runner.read_layouts = {"SRR3": [50, 50]}
    queue.scan_inbox()
    sample = drain(queue, state)["GSM3"]
    assert sample["status"] == "failed"
    assert "barcode read was not deposited" in sample["error"]


def test_atac_label_without_tool_paths_fails_clearly(tmp_path):
    table = "Run,Sample Name,Assay Type\nSRR3,GSM3,ATAC-seq\n"
    cfg, state, _runner, queue = project(
        tmp_path, table=table, runs=("SRR3",), atac_tools=False)
    queue.scan_inbox()
    sample = drain(queue, state)["GSM3"]
    assert sample["status"] == "failed"
    assert "paths.cellranger_atac_dir" in sample["error"]


def test_multiome_missing_a_library_fails_clearly(tmp_path):
    table = "Run,Sample Name,Assay Type,tech\nSRR5,GSM5,RNA-Seq,multiome\n"
    cfg, state, _runner, queue = project(tmp_path, table=table, runs=("SRR5",))
    queue.scan_inbox()
    sample = drain(queue, state)["GSM5"]
    assert sample["status"] == "failed" and "atac library" in sample["error"]


def test_pair_is_not_registered_when_a_member_already_exists(tmp_path):
    cfg, state, _runner, queue = project(tmp_path, table=TABLE.replace(",donorA", ","))
    queue.scan_inbox()
    assert state.get_sample("GSM5")
    cfg.data["metadata"]["multiome_pairs"] = {"donorA": ["GSM5", "GSM6"]}
    queue.scan_inbox()
    assert state.get_sample("donorA") is None


def test_label_change_applies_only_before_work_starts(tmp_path):
    table = "Run,Sample Name,Assay Type\nSRR1,GSM1,RNA-Seq\n"
    cfg, state, _runner, queue = project(tmp_path, table=table, runs=("SRR1",))
    queue.scan_inbox()
    cfg.data["metadata"]["tech"] = {"GSM1": "snrna"}
    queue.scan_inbox()
    assert state.get_sample("GSM1")["meta"]["tech"] == "snrna"
    drain(queue, state)
    cfg.data["metadata"]["tech"] = {"GSM1": "rna"}
    queue.scan_inbox()
    assert state.get_sample("GSM1")["meta"]["tech"] == "snrna"


def test_unlabelled_legacy_sample_is_rna():
    assert tech.sample_tech({"meta": {}}) == "rna"
    assert sra_table.parse_read_map("1=R1;2=R2;3=R3") == {1: "R1", 2: "R2", 3: "R3"}


def test_library_assignment_refreshes_for_untouched_multiome(tmp_path):
    table = ("Run,Sample Name,Assay Type,tech\n"
             "SRR5,GSM5,RNA-Seq,multiome\nSRR6,GSM5,RNA-Seq,multiome\n")
    cfg, state, _runner, queue = project(tmp_path, table=table, runs=("SRR5", "SRR6"))
    queue.scan_inbox()
    assert state.get_sample("GSM5")["meta"]["library_types"] == {"SRR5": "gex", "SRR6": "gex"}
    cfg.data["metadata"]["library_types"] = {"SRR6": "atac"}
    queue.scan_inbox()
    assert state.get_sample("GSM5")["meta"]["library_types"] == {"SRR5": "gex", "SRR6": "atac"}
    assert drain(queue, state)["GSM5"]["status"] == "done"


def test_label_fix_after_early_failure_takes_effect_on_retry(tmp_path):
    table = "Run,Sample Name,Assay Type\nSRR3,GSM3,RNA-Seq\n"   # ATAC reads, mislabelled
    cfg, state, runner, queue = project(tmp_path, table=table, runs=("SRR3",))
    queue.scan_inbox()
    assert drain(queue, state)["GSM3"]["status"] == "failed"
    cfg.data["metadata"]["tech"] = {"GSM3": "atac"}
    queue.scan_inbox()
    assert state.get_sample("GSM3")["meta"]["tech"] == "atac"
    assert not queue.retry("GSM3")
    done = drain(queue, state)["GSM3"]
    assert done["status"] == "done", done["error"]
    assert runner.calls[-1][0] == "cellranger-atac"
