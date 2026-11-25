"""Standalone sra-cellranger skill helper: snATAC and Multiome support."""
import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "sra-cellranger" / "scripts"


def load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inspect_reads = load("inspect_fastq_reads")
pipe = load("sra_cellranger_pipeline")


def args(tmp_path, **kw):
    base = dict(accessions=None, sample=None, sample_sheet=None, assay="rna",
                default_read_map="1=R1,2=R2", default_atac_read_map="1=R1,2=R2,3=R3",
                reference=tmp_path / "gex", atac_reference=tmp_path / "arc",
                cellranger="cellranger", cellranger_atac="cellranger-atac", cellranger_arc="cellranger-arc",
                create_bam=None, arc_legacy_bam_flag=False, localcores=8, localmem=64)
    base.update(kw)
    return argparse.Namespace(**base)


def sheet(tmp_path, text):
    path = tmp_path / "samples.csv"
    path.write_text(text)
    return path


def fastq(path, length, count=20):
    path.write_text("".join(f"@r{i}\n{'A' * length}\n+\n{'F' * length}\n" for i in range(count)))


@pytest.mark.parametrize("lengths,expected", [
    ({"1": 50, "2": 16, "3": 50}, "1=R1,2=R2,3=R3"),
    ({"1": 8, "2": 50, "3": 24, "4": 49}, "1=I1,2=R1,3=R2,4=R3"),
    ({"1": 16, "2": 50, "3": 50}, "1=R2,2=R1,3=R3"),
])
def test_atac_roles_follow_read_length(lengths, expected):
    assert inspect_reads.format_read_map(inspect_reads.infer_read_map(lengths, "atac")) == expected


def test_atac_without_barcode_read_is_rejected():
    with pytest.raises(ValueError, match="barcode read was not deposited"):
        inspect_reads.infer_read_map({"1": 50, "2": 50}, "atac")


def test_equal_length_gex_is_not_guessed():
    with pytest.raises(ValueError, match="explicit read_map"):
        inspect_reads.infer_read_map({"1": 150, "2": 150}, "rna")


def test_multiome_requires_both_library_types(tmp_path):
    a = args(tmp_path, sample_sheet=sheet(tmp_path, "accession,sample,assay,library_type\nSRR1,d1,multiome,gex\n"))
    with pytest.raises(ValueError, match="cannot run one modality alone"):
        pipe.load_samples(a)


def test_unquoted_comma_read_map_is_reported(tmp_path):
    a = args(tmp_path, sample_sheet=sheet(tmp_path, "accession,sample,assay,read_map\nSRR1,a1,atac,1=R1,2=R2,3=R3\n"))
    with pytest.raises(ValueError, match="semicolons"):
        pipe.load_samples(a)


def test_sheet_without_assay_column_stays_rna(tmp_path):
    a = args(tmp_path, sample_sheet=sheet(tmp_path, "accession,sample\nSRR1,s1\nSRR2,s1\n"))
    (sample,) = pipe.load_samples(a)
    assert sample.assay == "rna" and sample.accessions == ["SRR1", "SRR2"]


def test_multiome_staging_and_arc_command(tmp_path):
    a = args(tmp_path, sample_sheet=sheet(
        tmp_path,
        "accession,sample,assay,library_type,read_map\n"
        "SRRG,d1,multiome,Gene Expression,\n"
        "SRRA,d1,multiome,Chromatin Accessibility,auto\n"))
    (sample,) = pipe.load_samples(a)
    fq = tmp_path / "fastq"
    fq.mkdir()
    for name, length in {"SRRG_1": 28, "SRRG_2": 90, "SRRA_1": 8, "SRRA_2": 50, "SRRA_3": 24, "SRRA_4": 49}.items():
        fastq(fq / f"{name}.fastq", length)
        (fq / f"{name}.fastq").rename(fq / f"{name}.fastq.gz")  # plain text is enough for symlink staging
    # read_map=auto reads the files, so give the ATAC run real gzip content
    import gzip
    for name, length in {"SRRA_1": 8, "SRRA_2": 50, "SRRA_3": 24, "SRRA_4": 49}.items():
        with gzip.open(fq / f"{name}.fastq.gz", "wt") as handle:
            handle.write("".join(f"@r{i}\n{'A' * length}\n+\n{'F' * length}\n" for i in range(20)))
    staged_dir = tmp_path / "staged" / "d1"
    staged, libraries = pipe.stage_fastqs(sample, a, fq, staged_dir, dry_run=False)
    names = sorted(p.name for p in staged)
    assert len(set(names)) == len(names) == 6  # unique names, safe for zip -j
    assert (staged_dir / "atac" / "d1_atac_S1_L001_R2_001.fastq.gz").resolve().name == "SRRA_3.fastq.gz"
    assert libraries.read_text().splitlines() == [
        "fastqs,sample,library_type",
        f"{staged_dir / 'gex'},d1_gex,Gene Expression",
        f"{staged_dir / 'atac'},d1_atac,Chromatin Accessibility",
    ]
    runner = pipe.Runner(dry_run=True, log_dir=tmp_path / "logs")
    pipe.run_cellranger(sample, a, staged_dir, tmp_path / "work", runner, libraries)
    assert runner.commands[-1].startswith("cellranger-arc count --id=d1 ")
    assert "--create-bam=false" in runner.commands[-1] and f"--libraries={libraries}" in runner.commands[-1]


def test_atac_command_uses_atac_reference_and_no_expect_cells(tmp_path):
    a = args(tmp_path, sample_sheet=sheet(tmp_path, "accession,sample,assay,expected_cells\nSRR1,a1,atac,5000\n"))
    (sample,) = pipe.load_samples(a)
    runner = pipe.Runner(dry_run=True, log_dir=tmp_path / "logs")
    pipe.run_cellranger(sample, a, tmp_path / "staged", tmp_path / "work", runner)
    cmd = runner.commands[-1]
    assert cmd.startswith("cellranger-atac count") and f"--reference={tmp_path / 'arc'}" in cmd
    assert "--expect-cells" not in cmd and "--transcriptome" not in cmd
