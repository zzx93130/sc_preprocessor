"""SraRunTable.csv parsing: SRR -> GSM grouping with metadata.

Auto-discovery order (per the sra-auto-pipeline skill): explicit config path,
beside the inbox, inside the inbox, project root.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from . import tech as techmod

GSM_RE = re.compile(r"^GSM\d+$")
RUN_RE = re.compile(r"^[SED]RR\d+$")

# column names commonly holding the run accession / sample id in Run Selector exports
RUN_COLS = ("Run", "run", "acc", "Run_ID")
SAMPLE_COLS = ("Sample Name", "GEO_Accession (exp)", "GEO_Accession",
               "Sample_Name", "SampleName", "Library Name", "source_name")
# Optional columns the user may add to label the technology of a sample.
TECH_COLS = ("tech", "Tech", "technology", "Technology")
LIBRARY_COLS = ("library_type", "Library_Type", "library type")
MULTIOME_COLS = ("multiome_sample", "Multiome_Sample", "multiome sample")
# Run Selector columns used to recognise ATAC runs when no label is given.
ASSAY_COLS = ("Assay Type", "LibraryStrategy")


def _first(row: dict, cols) -> str:
    for col in cols:
        v = (row.get(col) or "").strip()
        if v:
            return v
    return ""


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
        label = _first(row, TECH_COLS)
        if label:
            meta.setdefault("tech_label", label)
        group = _first(row, MULTIOME_COLS)
        if group:
            meta.setdefault("multiome_sample", _sanitize(group))
        lib = _first(row, LIBRARY_COLS)
        if lib:
            lib = techmod.normalize_library(lib)
        else:
            assay = " ".join((row.get(c) or "") for c in ASSAY_COLS).upper()
            lib = techmod.ATAC_LIB if "ATAC" in assay else techmod.GEX_LIB
        meta.setdefault("run_library", {})[run] = lib

    for s in samples.values():
        s["srrs"] = sorted(set(s["srrs"]))
    if not samples:
        raise ValueError(f"no run accessions found in {path}")
    return samples


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def resolve_tech(groups: dict, cfg) -> dict:
    """Attach the technology label to parsed samples and build Multiome samples.

    Label precedence per sample: ``metadata.tech`` in config.yaml (keyed by
    sample id or run), a ``tech`` column in the SraRunTable, ATAC recognised
    from "Assay Type"/"LibraryStrategy", then ``pipeline.default_tech``.

    Multiome samples whose ATAC and GEX libraries are separate GSMs are merged
    under one id, from ``metadata.multiome_pairs`` ({new_id: [GSM, GSM]}) or a
    ``multiome_sample`` column shared by their rows.
    """
    cfg_tech = cfg.get("metadata", "tech", default={}) or {}
    cfg_libs = cfg.get("metadata", "library_types", default={}) or {}
    default = techmod.normalize_tech(
        cfg.get("pipeline", "default_tech", default=techmod.RNA) or techmod.RNA)

    pairs = {}
    for new_id, members in (cfg.get("metadata", "multiome_pairs", default={}) or {}).items():
        pairs[_sanitize(str(new_id))] = [str(m) for m in members]
    for sid, grp in groups.items():
        group = grp["meta"].get("multiome_sample")
        if group and sid not in {m for ms in pairs.values() for m in ms}:
            pairs.setdefault(group, []).append(sid)

    out = {}
    merged_members = set()
    for new_id, members in pairs.items():
        missing = [m for m in members if m not in groups]
        if missing:
            raise ValueError(
                f"multiome sample {new_id}: {', '.join(missing)} not in the SraRunTable")
        meta = {"tech": techmod.MULTIOME, "multiome_members": list(members),
                "run_library": {}, "run_bytes": {}}
        srrs = []
        for m in members:
            src = groups[m]
            srrs += src["srrs"]
            meta["run_library"].update(src["meta"].get("run_library", {}))
            meta["run_bytes"].update(src["meta"].get("run_bytes", {}))
            for k, v in src["meta"].items():
                if k not in ("run_library", "run_bytes", "tech_label",
                             "multiome_sample", "gsm", "bytes"):
                    meta.setdefault(k, v)
        if not meta["run_bytes"]:
            del meta["run_bytes"]
        out[new_id] = {"srrs": sorted(set(srrs)), "meta": meta}
        merged_members.update(members)

    for sid, grp in groups.items():
        if sid in merged_members:
            continue
        meta = dict(grp["meta"])
        libs = set(meta.get("run_library", {}).values())
        label = cfg_tech.get(sid) or next(
            (cfg_tech[r] for r in grp["srrs"] if r in cfg_tech), "") \
            or meta.get("tech_label")
        if label:
            meta["tech"] = techmod.normalize_tech(label)
        elif libs == {techmod.ATAC_LIB}:
            meta["tech"] = techmod.ATAC
        elif libs == {techmod.ATAC_LIB, techmod.GEX_LIB}:
            meta["tech"] = techmod.MULTIOME  # both libraries under one GSM
        else:
            meta["tech"] = default
        out[sid] = {"srrs": grp["srrs"], "meta": meta}

    for sid, grp in out.items():
        meta = grp["meta"]
        run_library = meta.pop("run_library", {})
        meta.pop("tech_label", None)
        meta.pop("multiome_sample", None)
        if meta["tech"] == techmod.MULTIOME:
            libs = {r: run_library.get(r, techmod.GEX_LIB) for r in grp["srrs"]}
            for r in grp["srrs"]:
                if r in cfg_libs:
                    libs[r] = techmod.normalize_library(cfg_libs[r])
            meta["library_types"] = libs
    return out


def parse_read_map(spec: str) -> dict:
    """'1=I1,2=R1,3=R2' -> {1: 'I1', 2: 'R1', 3: 'R2'}"""
    out = {}
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        idx, role = part.split("=")
        role = role.strip().upper()
        if role not in ("I1", "I2", "R1", "R2", "R3", "SKIP"):
            raise ValueError(f"bad read role {role!r} in {spec!r}")
        out[int(idx)] = role
    return out
