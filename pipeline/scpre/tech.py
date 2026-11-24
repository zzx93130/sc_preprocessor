"""Per-sample technology label.

Every sample carries ``meta["tech"]``:

    rna       sc/snRNA-seq gene expression     cellranger count   (default)
    snrna     single-nucleus RNA-seq           cellranger count --include-introns=true
    atac      sc/snATAC-seq                    cellranger-atac count
    multiome  10x Multiome ATAC + GEX          cellranger-arc count

A multiome sample also carries ``meta["library_types"]`` mapping each run to
``gex`` or ``atac``. Samples registered before this label existed have no
``tech`` key and are treated as ``rna``, so their behaviour is unchanged.
"""
from __future__ import annotations

from pathlib import Path

RNA, SNRNA, ATAC, MULTIOME = "rna", "snrna", "atac", "multiome"
TECHS = (RNA, SNRNA, ATAC, MULTIOME)
GEX_LIB, ATAC_LIB = "gex", "atac"

TECH_ALIASES = {
    "rna": RNA, "gex": RNA, "scrna": RNA, "scrna-seq": RNA, "scrnaseq": RNA,
    "rna-seq": RNA, "3prime": RNA, "5prime": RNA,
    "snrna": SNRNA, "snrna-seq": SNRNA, "snrnaseq": SNRNA, "nuclei": SNRNA,
    "atac": ATAC, "snatac": ATAC, "scatac": ATAC, "atac-seq": ATAC,
    "snatac-seq": ATAC, "scatac-seq": ATAC,
    "multiome": MULTIOME, "arc": MULTIOME, "atac+gex": MULTIOME,
    "gex+atac": MULTIOME, "multiome-atac-gex": MULTIOME,
}
LIBRARY_ALIASES = {
    "gex": GEX_LIB, "rna": GEX_LIB, "snrna": GEX_LIB, "scrna": GEX_LIB,
    "gene expression": GEX_LIB, "rna-seq": GEX_LIB,
    "atac": ATAC_LIB, "snatac": ATAC_LIB, "scatac": ATAC_LIB,
    "chromatin accessibility": ATAC_LIB, "atac-seq": ATAC_LIB,
}
ARC_LIBRARY_NAME = {GEX_LIB: "Gene Expression", ATAC_LIB: "Chromatin Accessibility"}

# Which paths.* keys a tech needs: (tool directory, executable, reference).
TOOLS = {
    RNA: ("cellranger_dir", "cellranger", "reference_dir"),
    SNRNA: ("cellranger_dir", "cellranger", "reference_dir"),
    ATAC: ("cellranger_atac_dir", "cellranger-atac", "atac_reference_dir"),
    MULTIOME: ("cellranger_arc_dir", "cellranger-arc", "atac_reference_dir"),
}


def normalize_tech(value) -> str:
    """Return the canonical tech for a label, or raise ValueError."""
    key = str(value or "").strip().lower().replace("_", "-").replace(" ", "")
    if key not in TECH_ALIASES:
        raise ValueError(
            f"unknown tech label {value!r}; use one of {', '.join(TECHS)}")
    return TECH_ALIASES[key]


def normalize_library(value) -> str:
    key = str(value or "").strip().lower().replace("_", " ")
    if key not in LIBRARY_ALIASES:
        raise ValueError(
            f"unknown library type {value!r}; use gex or atac")
    return LIBRARY_ALIASES[key]


def sample_tech(sample: dict) -> str:
    return (sample.get("meta") or {}).get("tech") or RNA


def is_gex_only(tech: str) -> bool:
    return tech in (RNA, SNRNA)


def library_type(sample: dict, srr: str) -> str:
    """Library type of one run of a sample."""
    tech = sample_tech(sample)
    if tech == ATAC:
        return ATAC_LIB
    if tech == MULTIOME:
        libs = (sample.get("meta") or {}).get("library_types") or {}
        if srr not in libs:
            raise ValueError(
                f"multiome sample {sample.get('id')}: run {srr} has no library "
                "type; set metadata.library_types in config.yaml, e.g. "
                f'{{"{srr}": "atac"}}')
        return libs[srr]
    return GEX_LIB


def fastq_prefix(sample_id: str, tech: str, lib: str) -> str:
    """FASTQ name prefix (the Cell Ranger --sample value) for one library.

    Multiome keeps both libraries in one flat directory, told apart by prefix,
    so the FASTQ zip never has two members with the same name.
    """
    return f"{sample_id}_{lib}" if tech == MULTIOME else sample_id


def sample_prefixes(sample: dict) -> dict:
    """{library type: fastq prefix} for the libraries this sample must have."""
    tech = sample_tech(sample)
    libs = {MULTIOME: (GEX_LIB, ATAC_LIB), ATAC: (ATAC_LIB,)}.get(tech, (GEX_LIB,))
    return {lib: fastq_prefix(sample["id"], tech, lib) for lib in libs}


def required_roles(lib: str) -> set:
    return {"R1", "R2", "R3"} if lib == ATAC_LIB else {"R1", "R2"}


def assign_atac_roles(lengths: dict) -> dict:
    """Assign 10x ATAC roles from median read lengths.

    lengths: {path: median length}, any hashable keys that sort in SRA read
    order. R1/R3 are the genomic reads, R2 is the i5 cell-barcode read (16 bp,
    24 bp for Multiome ATAC), I1 the optional 8 bp sample index. Raises
    ValueError rather than guessing an ambiguous layout.
    """
    ordered = sorted(lengths)
    index = [f for f in ordered if lengths[f] <= 10]
    barcode = [f for f in ordered if 16 <= lengths[f] <= 24]
    genomic = [f for f in ordered if lengths[f] >= 30]
    if (len(barcode) != 1 or len(genomic) != 2
            or len(index) + len(barcode) + len(genomic) != len(ordered)):
        shown = {getattr(f, "name", str(f)): n for f, n in lengths.items()}
        raise ValueError(
            f"cannot assign ATAC read roles from lengths {shown}: need one "
            "16-24 bp barcode read and two genomic reads. If only two reads "
            "exist the barcode read was not deposited as FASTQ; otherwise set "
            'metadata.read_maps for this run, e.g. "SRR123": "1=R1,2=R2,3=R3"')
    roles = {genomic[0]: "R1", barcode[0]: "R2", genomic[1]: "R3"}
    for i, f in enumerate(index):
        roles[f] = "I1" if i == 0 else "SKIP"
    return roles


# -- outputs ---------------------------------------------------------------
# Files (relative to outs/) that must exist and be non-empty for the pipeline
# run to count as complete. The first entry after web_summary.html is also
# checksum-compared between the working copy and the archive.
REQUIRED_OUTS = {
    RNA: ["web_summary.html",
          "filtered_feature_bc_matrix/matrix.mtx.gz",
          "filtered_feature_bc_matrix/barcodes.tsv.gz",
          "filtered_feature_bc_matrix/features.tsv.gz"],
    ATAC: ["web_summary.html",
           "fragments.tsv.gz",
           "fragments.tsv.gz.tbi",
           "filtered_peak_bc_matrix.h5",
           "summary.csv"],
    MULTIOME: ["web_summary.html",
               "atac_fragments.tsv.gz",
               "atac_fragments.tsv.gz.tbi",
               "filtered_feature_bc_matrix.h5",
               "summary.csv"],
}
REQUIRED_OUTS[SNRNA] = REQUIRED_OUTS[RNA]

# BAM files written by each tool, used for optional exclusion from the archive.
BAM_GLOBS = {
    ATAC: ("possorted_bam.bam", "possorted_bam.bam.bai"),
    MULTIOME: ("atac_possorted_bam.bam", "atac_possorted_bam.bam.bai",
               "gex_possorted_bam.bam", "gex_possorted_bam.bam.bai"),
}


def required_outs(tech: str) -> list:
    return list(REQUIRED_OUTS[tech])


def checksum_out(tech: str) -> str:
    return REQUIRED_OUTS[tech][1]


def outs_valid(outs: Path, tech: str) -> bool:
    for rel in REQUIRED_OUTS[tech]:
        f = outs / rel
        if not f.is_file() or f.stat().st_size == 0:
            return False
    return True


def tool_paths(cfg, tech: str) -> tuple:
    """(tool dir, reference dir) for a tech; ValueError when not configured."""
    tool_key, exe, ref_key = TOOLS[tech]
    out = []
    for key, what in ((tool_key, f"{exe} installation"), (ref_key, "reference")):
        raw = cfg.get("paths", key, default="")
        if not raw:
            raise ValueError(
                f"tech {tech!r} needs paths.{key} ({what}) in config.yaml")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise ValueError(f"paths.{key} is not a directory: {path}")
        out.append(path)
    return out[0], out[1]
