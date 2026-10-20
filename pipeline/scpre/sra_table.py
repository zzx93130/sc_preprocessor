"""SraRunTable.csv parsing: SRR -> GSM grouping with metadata.

Auto-discovery order (per the sra-auto-pipeline skill): explicit config path,
beside the inbox, inside the inbox, project root.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

GSM_RE = re.compile(r"^GSM\d+$")
RUN_RE = re.compile(r"^[SED]RR\d+$")

# column names commonly holding the run accession / sample id in Run Selector exports
RUN_COLS = ("Run", "run", "acc", "Run_ID")
SAMPLE_COLS = ("Sample Name", "GEO_Accession (exp)", "GEO_Accession",
               "Sample_Name", "SampleName", "Library Name", "source_name")


def discover(root: Path, inbox: Path, explicit: str = "") -> Path:
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    for base in (inbox.parent, inbox, root):
        cands.append(base / "SraRunTable.csv")
        cands.append(base / "SraRunTable.txt")
    for c in cands:
        if c.is_file():
            return c
    return None


def parse(path: Path) -> dict:
    """Return {sample_id: {"srrs": [...], "meta": {...}}} grouped by GSM.

    The sample id is the GSM accession when one is found in the row; otherwise
    the value of the first sample-ish column; otherwise the run accession
    itself (one sample per run).
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path} is empty")

    samples = {}
    for row in rows:
        run = None
        for col in RUN_COLS:
            v = (row.get(col) or "").strip()
            if RUN_RE.match(v):
                run = v
                break
        if run is None:  # scan all values as fallback
            for v in row.values():
                v = (v or "").strip()
                if RUN_RE.match(v):
                    run = v
                    break
        if run is None:
            continue

        gsm = None
        for col in SAMPLE_COLS:
            v = (row.get(col) or "").strip()
            if GSM_RE.match(v):
                gsm = v
                break
        if gsm is None:
            for v in row.values():
                v = (v or "").strip()
                if GSM_RE.match(v):
                    gsm = v
                    break
        sample_id = gsm
        if sample_id is None:
            for col in SAMPLE_COLS:
                v = (row.get(col) or "").strip()
                if v:
                    sample_id = _sanitize(v)
                    break
        if sample_id is None:
            sample_id = run

        entry = samples.setdefault(sample_id, {"srrs": [], "meta": {}})
        entry["srrs"].append(run)
        meta = entry["meta"]
        try:
            run_bytes = int((row.get("Bytes") or "0").strip())
        except ValueError:
            run_bytes = 0
        if run_bytes > 0:
            meta.setdefault("run_bytes", {})[run] = run_bytes
        for src, dst in (("Organism", "organism"),
                         ("Instrument", "instrument"),
                         ("LibraryLayout", "library_layout"),
                         ("LibraryStrategy", "library_strategy"),
                         ("LibrarySource", "library_source"),
                         ("Bytes", "bytes"),
                         ("BioSample", "biosample"),
                         ("SRA Study", "sra_study"),
                         ("GEO_Accession (exp)", "gsm"),
                         ("Sample Name", "sample_name")):
            v = (row.get(src) or "").strip()
            if v and dst not in meta:
                meta[dst] = v
        if gsm:
            meta.setdefault("gsm", gsm)

    for s in samples.values():
        s["srrs"] = sorted(set(s["srrs"]))
    if not samples:
        raise ValueError(f"no run accessions found in {path}")
    return samples


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def parse_read_map(spec: str) -> dict:
    """'1=I1,2=R1,3=R2' -> {1: 'I1', 2: 'R1', 3: 'R2'}"""
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        idx, role = part.split("=")
        role = role.strip().upper()
        if role not in ("I1", "I2", "R1", "R2", "SKIP"):
            raise ValueError(f"bad read role {role!r} in {spec!r}")
        out[int(idx)] = role
    return out
