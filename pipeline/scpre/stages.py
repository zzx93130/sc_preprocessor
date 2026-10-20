"""Per-sample stage machine. Every stage is idempotent and resumable:
a stage writes a validated sentinel (.stages/<stage>.json) on success and is
skipped on re-run only when the sentinel's recorded outputs still validate.
"""
from __future__ import annotations

import gzip
import json
import shutil
import time
import zipfile
from functools import lru_cache
from pathlib import Path

from . import repro
from .docker_run import ToolError
from .sra_table import parse_read_map
from .util import gzip_ok, path_available, rm_rf, sha256_file

STAGES = ["downloading", "converting", "cellranger", "loom",
          "archiving", "verifying", "cleaning"]

DOCKER_STAGES = {"downloading", "converting", "cellranger", "loom"}


class PauseError(RuntimeError):
    """Environment (drive/docker) not ready — pause, don't fail the sample."""


class ShutdownError(RuntimeError):
    """The service is stopping; preserve progress and requeue the sample."""


class StageContext:
    def __init__(self, cfg, state, runner, sample: dict, logger):
        self.cfg = cfg
        self.state = state
        self.runner = runner
        self.sample = sample
        self.log = logger

    # -- layout -----------------------------------------------------------
    @property
    def sid(self) -> str:
        return self.sample["id"]

    @property
    def work(self) -> Path:
        return self.cfg.work_dir / self.sid

    @property
    def sra_dir(self) -> Path:
        return self.work / "sra"

    @property
    def fastq_dir(self) -> Path:
        return self.work / "fastq"

    @property
    def cr_dir(self) -> Path:
        return self.work / "cellranger"

    @property
    def loom_dir(self) -> Path:
        return self.work / "loom"

    @property
    def stages_dir(self) -> Path:
        return self.work / ".stages"

    @property
    def outs(self) -> Path:
        return self.cr_dir / self.sid / "outs"

    @property
    def archive(self) -> Path:
        return self.cfg.archive_sample_dir(self.sid)

    # -- sentinels --------------------------------------------------------
    def sentinel(self, stage: str) -> Path:
        return self.stages_dir / f"{stage}.json"

    def sentinel_write(self, stage: str, outputs) -> None:
        self.stages_dir.mkdir(parents=True, exist_ok=True)
        rec = {"completed_at": time.time(), "outputs": {}}
        for p in outputs:
            p = Path(p)
            rec["outputs"][str(p)] = p.stat().st_size if p.exists() else -1
        self.sentinel(stage).write_text(json.dumps(rec, indent=1))

    def sentinel_valid(self, stage: str) -> bool:
        s = self.sentinel(stage)
        if not s.exists():
            return False
        try:
            rec = json.loads(s.read_text())
        except (ValueError, OSError):
            return False
        for path, size in rec.get("outputs", {}).items():
            p = Path(path)
            if not p.exists() or (size >= 0 and p.stat().st_size != size):
                return False
        return True

    def refresh(self) -> None:
        self.sample = self.state.get_sample(self.sid)


# --------------------------------------------------------------------------
def ensure_ready(ctx: StageContext, stage: str) -> None:
    """Raise PauseError when a required drive or Docker is unavailable."""
    if not path_available(ctx.cfg.path("work_root")):
        raise PauseError(f"working drive unavailable: {ctx.cfg.path('work_root')}")
    if stage in ("archiving", "verifying", "cleaning") and \
            not path_available(ctx.cfg.path("archive_root")):
        raise PauseError(f"archive drive unavailable: {ctx.cfg.path('archive_root')}")
    if stage in DOCKER_STAGES and not ctx.runner.healthy():
        raise PauseError("Docker daemon unavailable")


def resolve_sra_inputs(cfg, srr: str) -> Path:
    """Find an existing .sra for an accession in the inbox (several layouts)."""
    inbox = cfg.path("inbox_dir")
    for cand in (inbox / srr, inbox / f"{srr}.sra",
                 inbox / srr / f"{srr}.sra"):
        if cand.is_file():
            return cand
    return None


# --------------------------------------------------------------------------
# Stage implementations. Each returns a list of output paths for its sentinel.

def stage_downloading(ctx: StageContext):
    if ctx.sample["mode"] == "fastq":
        return []
    resolved = {}
    for srr in ctx.sample["srrs"]:
        p = resolve_sra_inputs(ctx.cfg, srr)
        if p is None:
            p = ctx.sra_dir / f"{srr}.sra"
            if not p.is_file():
                ctx.log.info("downloading %s via prefetch", srr)
                p = ctx.runner.prefetch(srr, ctx.sra_dir, ctx.sid, ctx.log)
            else:
                ctx.log.info("%s already downloaded", srr)
        else:
            ctx.log.info("%s found in inbox: %s", srr, p)
        resolved[srr] = str(p)
    ctx.state.update_meta(ctx.sid, input_paths=list(resolved.values()),
                          sra_paths=resolved)
    return list(resolved.values())


ROLE_BY_LEN = ((16, "I1"), (32, "R1"), (10 ** 9, "R2"))


def _read_stats(fastq_path: Path, n_reads: int = 500) -> dict:
    """Median read length + T-richness after the CB+UMI window (pos 29..60).

    10x GEX R1 = 16bp barcode + 12bp UMI + poly(dT): strongly T-rich past
    position 28 even when R1 was sequenced to full 101/151bp. R2 (cDNA) is
    not. This distinguishes the reads when their lengths are identical.
    """
    lens, t_count, t_total = [], 0, 0
    with open(fastq_path) as f:
        for _ in range(n_reads):
            header = f.readline()
            if not header:
                break
            seq = f.readline().strip()
            f.readline()
            f.readline()
            lens.append(len(seq))
            seg = seq[28:60]
            t_count += seg.count("T")
            t_total += len(seg)
    if not lens:
        raise ToolError(f"{fastq_path} contains no reads")
    lens.sort()
    return {"median_len": lens[len(lens) // 2],
            "t_frac": (t_count / t_total) if t_total else 0.0}


@lru_cache(maxsize=2)
def _barcode_whitelist(path: Path):
    return frozenset(path.read_text().splitlines())


def _assign_roles(files, logger, barcode_whitelist=None, decision=None) -> dict:
    """Jointly assign I1/I2/R1/R2/SKIP to a run's split files.

    Guarantees each role is used at most once; raises with a read_maps hint
    instead of ever guessing ambiguously (a wrong guess silently corrupts
    the sample, as a duplicate-name overwrite would).
    """
    stats = {f: _read_stats(f) for f in files}
    for f, s in stats.items():
        logger.info("  %s: median_len=%d T-frac[29:60]=%.2f",
                    f.name, s["median_len"], s["t_frac"])
    roles = {}
    index_files = [f for f in files if stats[f]["median_len"] <= 16]
    long_files = [f for f in files if stats[f]["median_len"] > 16]
    for i, f in enumerate(sorted(index_files)):
        roles[f] = "I1" if i == 0 else ("I2" if i == 1 else "SKIP")

    if len(long_files) < 2:
        raise ToolError(
            f"expected at least R1+R2 among {[f.name for f in files]} "
            f"(lengths {[stats[f]['median_len'] for f in files]}); "
            "set metadata.read_maps for this run in config.yaml, e.g. "
            '"SRR123": "1=R1,2=R2"')

    by_t = sorted(long_files, key=lambda f: stats[f]["t_frac"], reverse=True)
    top, second = by_t[0], by_t[1]
    r1 = None
    if stats[top]["t_frac"] >= 0.45 and stats[second]["t_frac"] <= 0.30:
        r1 = top  # unambiguous poly(dT) signal
    else:
        # fall back to length only when clearly different
        by_len = sorted(long_files, key=lambda f: stats[f]["median_len"])
        if stats[by_len[1]]["median_len"] - stats[by_len[0]]["median_len"] >= 20:
            r1 = by_len[0]
    if r1 is None:
        if barcode_whitelist and Path(barcode_whitelist).is_file():
            whitelist = _barcode_whitelist(Path(barcode_whitelist))
            matches = {}
            for path in long_files:
                reads = hits = 0
                with path.open() as fh:
                    for _ in range(1000):
                        header = fh.readline()
                        if not header:
                            break
                        seq = fh.readline().strip()
                        plus, qual = fh.readline(), fh.readline().strip()
                        if not header.startswith("@") or not plus.startswith("+") or len(seq) != len(qual):
                            raise ToolError(f"invalid sampled FASTQ record in {path}")
                        reads += 1
                        hits += seq[:16] in whitelist
                matches[path] = {"reads": reads, "matches": hits,
                                 "fraction": hits / reads if reads else 0}
                logger.info("barcode whitelist %s: %s first16 matches %d/%d",
                            Path(barcode_whitelist).name, path.name, hits, reads)
            ranked = sorted(long_files, key=lambda path: matches[path]["fraction"], reverse=True)
            best, runner_up = ranked[:2]
            if (all(matches[path]["reads"] >= 200 for path in long_files)
                    and matches[best]["fraction"] >= 0.60
                    and matches[runner_up]["fraction"] <= 0.05):
                r1 = best
                if decision is not None:
                    decision.update(method="barcode_whitelist_first16",
                                    whitelist=str(barcode_whitelist),
                                    whitelist_sha256=sha256_file(Path(barcode_whitelist)),
                                    evidence={path.name: rec for path, rec in matches.items()})
                logger.info("R1 selected by strong barcode whitelist evidence: %s", r1.name)
    if r1 is None:
        raise ToolError(
            "cannot distinguish R1 from R2 for "
            f"{[(f.name, stats[f]['median_len'], round(stats[f]['t_frac'], 2)) for f in long_files]}"
            " — no clear poly(dT) or length signal. Set metadata.read_maps "
            'for this run in config.yaml, e.g. "SRR123": "1=R1,2=R2"')
    roles[r1] = "R1"
    remaining = [f for f in long_files if f is not r1]
    r2 = max(remaining, key=lambda f: stats[f]["median_len"])
    roles[r2] = "R2"
    for f in remaining:
        if f is not r2:
            roles[f] = "SKIP"
    return roles


def stage_converting(ctx: StageContext):
    ctx.fastq_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    if ctx.sample["mode"] == "fastq":
        outputs = _stage_fastq_ingest(ctx)
    else:
        outputs = _stage_sra_convert(ctx)
    # validate
    r1 = [p for p in outputs if "_R1_" in Path(p).name]
    r2 = [p for p in outputs if "_R2_" in Path(p).name]
    if not r1 or not r2:
        raise ToolError(
            f"converting produced no R1/R2 pair for {ctx.sid}: {outputs}")
    for p in outputs:
        if not gzip_ok(Path(p)):
            raise ToolError(f"gzip validation failed for {p}")
    return outputs


def _stage_sra_convert(ctx: StageContext):
    read_maps = ctx.cfg.get("metadata", "read_maps", default={}) or {}
    tmp = ctx.work / "fq_tmp"
    outputs = []
    total_spots = 0
    sra_paths = ctx.sample["meta"].get("sra_paths", {})
    for lane, srr in enumerate(ctx.sample["srrs"], start=1):
        lane_tag = f"L{lane:03d}"
        lane_prefix = f"{ctx.sid}_S1_{lane_tag}_"
        expected = sorted(
            ctx.fastq_dir.glob(f"{lane_prefix}*_001.fastq.gz")
        )
        staged = sorted(ctx.fastq_dir.glob(f"{lane_prefix}*_001.fastq"))
        staged_roles = {p.name.split("_")[-2] for p in staged}
        valid_gz = []
        for path in expected:
            role = path.name.split("_")[-2]
            # gzip keeps its source until compression succeeds. If both are
            # present after a shutdown, the .gz is partial and must be redone.
            if role in staged_roles:
                rm_rf(path)
            elif gzip_ok(path):
                valid_gz.append(path)
            else:
                rm_rf(path)
        roles_present = staged_roles | {
            p.name.split("_")[-2] for p in valid_gz
        }
        if {"R1", "R2"} <= roles_present:
            if staged:
                ctx.log.info(
                    "lane %s (%s) has completed uncompressed reads; "
                    "resuming compression", lane_tag, srr,
                )
                valid_gz += ctx.runner.compress(
                    staged,
                    int(ctx.cfg.get(
                        "concurrency", "convert_threads", default=6
                    )),
                    ctx.sid, ctx.log,
                )
            else:
                ctx.log.info("lane %s (%s) already converted", lane_tag, srr)
            outputs += [str(p) for p in sorted(valid_gz)]
            rm_rf(ctx.work / "raw_fastq" / srr)
            continue
        for p in ctx.fastq_dir.glob(f"{ctx.sid}_S1_{lane_tag}_*"):
            rm_rf(p)  # clean partial lane
        sra = Path(sra_paths.get(srr) or resolve_sra_inputs(ctx.cfg, srr)
                   or (ctx.sra_dir / f"{srr}.sra"))
        if not sra.is_file():
            raise ToolError(f"input .sra missing for {srr}: {sra}")
        raw_dir = ctx.work / "raw_fastq" / srr
        cached = _validated_raw_manifest(raw_dir, srr)
        if cached:
            produced, spots = cached
            ctx.log.info("reusing completed raw FASTQs for %s", srr)
        else:
            rm_rf(raw_dir)
            produced, spots = ctx.runner.fasterq(
                sra, raw_dir, tmp,
                int(ctx.cfg.get("concurrency", "convert_threads", default=6)),
                ctx.sid, ctx.log)
            _write_raw_manifest(raw_dir, srr, produced, spots)
        if spots:
            total_spots += spots
        # assign roles: explicit readMap wins, else joint content detection
        rm_spec = read_maps.get(srr)
        produced = sorted(produced)
        if rm_spec:
            role_map = parse_read_map(rm_spec)
            roles = {}
            for p in produced:
                stem = p.stem  # SRRxxx_1
                idx = int(stem.rsplit("_", 1)[1]) if "_" in stem else 1
                roles[p] = role_map.get(idx, "SKIP")
            ctx.log.info("read roles from metadata.read_maps: %s",
                         {p.name: r for p, r in roles.items()})
        else:
            decision = {}
            whitelist = (ctx.cfg.path("cellranger_dir") / "lib/python/cellranger/barcodes/737K-august-2016.txt")
            roles = _assign_roles(produced, ctx.log, whitelist, decision)
            if decision:
                decision.update(run=srr, sample=ctx.sid,
                                roles={p.name: role for p, role in roles.items()})
                decisions = dict(ctx.state.get_sample(ctx.sid)["meta"].get("read_role_decisions", {}))
                decisions[srr] = decision
                ctx.state.update_meta(ctx.sid, read_role_decisions=decisions)
                ctx.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
                (ctx.cfg.reports_dir / f"{ctx.sid}.{srr}.read-roles.json").write_text(json.dumps(decision, indent=2))
        # safety: a duplicate destination would silently overwrite a read
        assigned = [r for r in roles.values() if r and r != "SKIP"]
        if len(assigned) != len(set(assigned)):
            raise ToolError(f"role assignment collision for {srr}: "
                            f"{ {p.name: r for p, r in roles.items()} }")
        renamed = []
        for p in produced:
            role = roles.get(p)
            if role is None or role == "SKIP":
                ctx.log.info("skipping %s (role SKIP)", p.name)
                p.unlink()
                continue
            dest = ctx.fastq_dir / f"{ctx.sid}_S1_{lane_tag}_{role}_001.fastq"
            ctx.log.info("%s -> %s", p.name, dest.name)
            shutil.move(str(p), str(dest))
            renamed.append(dest)
        gzs = ctx.runner.compress(
            renamed, int(ctx.cfg.get("concurrency", "convert_threads", default=6)),
            ctx.sid, ctx.log)
        outputs += [str(g) for g in gzs]
        rm_rf(raw_dir)
    if total_spots:
        ctx.state.update_meta(ctx.sid, read_counts=total_spots)
    rm_rf(tmp)
    return outputs


def _raw_manifest_path(raw_dir: Path) -> Path:
    return raw_dir / ".fasterq-complete.json"


def _write_raw_manifest(raw_dir: Path, srr: str, produced, spots) -> None:
    """Record fasterq success before read-role analysis or compression.

    Role-map errors happen after an expensive, successful conversion. This
    small manifest lets Retry reuse those raw FASTQs while still rejecting
    files left by a killed or failed fasterq process.
    """
    rec = {"srr": srr, "spots": spots, "files": []}
    for path in produced:
        path = Path(path)
        rec["files"].append({"name": path.name, "size": path.stat().st_size})
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = _raw_manifest_path(raw_dir)
    part = dest.with_suffix(".json.part")
    part.write_text(json.dumps(rec, indent=1))
    part.replace(dest)


def _validated_raw_manifest(raw_dir: Path, srr: str):
    path = _raw_manifest_path(raw_dir)
    try:
        rec = json.loads(path.read_text())
        if rec.get("srr") != srr or len(rec.get("files", [])) < 2:
            return None
        files = []
        for item in rec["files"]:
            name = item["name"]
            if Path(name).name != name or not name.startswith(srr):
                return None
            p = raw_dir / name
            if not p.is_file() or p.stat().st_size != int(item["size"]):
                return None
            files.append(p)
        return sorted(files), rec.get("spots")
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _stage_fastq_ingest(ctx: StageContext):
    """FASTQ mode: stage user-provided FASTQs under Cell Ranger naming."""
    inputs = [Path(p) for p in ctx.sample["meta"].get("input_paths", [])]
    if not inputs:
        raise ToolError(f"fastq mode but no input_paths for {ctx.sid}")
    outputs = []
    lanes = {}
    for p in sorted(inputs):
        name = p.name
        role = None
        for r in ("R1", "R2", "I1", "I2"):
            if f"_{r}_" in name or name.endswith(f"_{r}.fastq.gz") \
                    or f"_{r}." in name:
                role = r
                break
        if role is None:
            role = _detect_role_gz(p)
        import re
        m = re.search(r"_L(\d{3})_", name)
        lane = m.group(1) if m else "001"
        lanes.setdefault((lane, role), []).append(p)
    used_lanes = {}
    for (lane, role), paths in sorted(lanes.items()):
        for i, p in enumerate(paths):
            # several files mapping to the same (lane, role) get distinct lanes
            eff_lane = lane if i == 0 else f"{int(lane) + 100 * i:03d}"
            key = (eff_lane, role)
            while key in used_lanes:
                eff_lane = f"{int(eff_lane) + 1:03d}"
                key = (eff_lane, role)
            used_lanes[key] = p
            dest = ctx.fastq_dir / f"{ctx.sid}_S1_L{eff_lane}_{role}_001.fastq.gz"
            if dest.exists() and dest.stat().st_size == p.stat().st_size:
                outputs.append(str(dest))
                continue
            ctx.log.info("staging %s -> %s", p, dest.name)
            shutil.copy2(p, dest)
            outputs.append(str(dest))
    return outputs


def _detect_role_gz(path: Path) -> str:
    with gzip.open(path, "rt") as f:
        f.readline()
        seq = f.readline().strip()
    n = len(seq)
    for limit, role in ROLE_BY_LEN:
        if n <= limit:
            return role
    return "R2"


def stage_cellranger(ctx: StageContext):
    cc = ctx.cfg.data["concurrency"]
    if ctx.outs.is_dir() and _validate_outs(ctx):
        ctx.log.info("cellranger outputs already valid — skipping")
    else:
        pipestance = ctx.cr_dir / ctx.sid
        # A service/drive interruption can leave a valid Martian pipestance
        # with an orphaned timestamp lock.  On a Unix-inode filesystem,
        # Cell Ranger can resume it when the same invocation is started again.
        # Failed samples still go through clean_partial(), which removes the
        # pipestance before an explicit retry; this path is only for automatic
        # recovery of an interrupted active/waiting sample.
        if (pipestance / "_invocation").is_file() and \
                not (pipestance / "_finalstate").is_file():
            lock = pipestance / "_lock"
            if lock.exists():
                lock.unlink()
            ctx.log.info("interrupted Cell Ranger pipestance found — resuming")
        else:
            rm_rf(pipestance)
        analysis_fastqs = ctx.fastq_dir
        policies = ctx.cfg.get("metadata", "paired_only_runs", default={}) or {}
        lanes = {f"L{i:03d}": dict(policies[srr], run=srr)
                 for i, srr in enumerate(ctx.sample["srrs"], 1) if srr in policies}
        if lanes:
            from .paired_input import prepare
            analysis_fastqs = ctx.work / "cellranger_fastq"
            decision = prepare(ctx.fastq_dir, analysis_fastqs, ctx.sid, lanes, ctx.log)
            ctx.state.update_meta(ctx.sid, paired_input_decision=decision)
            ctx.sample["meta"]["paired_input_decision"] = decision
            ctx.log.info("Using audited paired-only analysis inputs; original FASTQs retained")
        ctx.runner.cellranger_count(
            ctx.sid, analysis_fastqs, ctx.cr_dir, ctx.cfg.path("reference_dir"),
            int(cc["cellranger_cores"]), int(cc["cellranger_mem_gb"]),
            bool(ctx.cfg.get("pipeline", "create_bam", default=True)), ctx.log)
        if not _validate_outs(ctx):
            raise ToolError(f"cellranger outputs failed validation in {ctx.outs}")
    ctx.state.update_meta(
        ctx.sid,
        cellranger_version=ctx.sample["meta"].get("cellranger_version")
        or _cellranger_version(ctx),
        reference=str(ctx.cfg.path("reference_dir")))
    key = [ctx.outs / "web_summary.html", ctx.outs / "metrics_summary.csv"]
    key += list((ctx.outs / "filtered_feature_bc_matrix").glob("*"))
    return [str(p) for p in key]


def _cellranger_version(ctx) -> str:
    vfile = ctx.cfg.path("cellranger_dir") / ".version"
    try:
        return vfile.read_text().strip()
    except OSError:
        return "unknown"


def _validate_outs(ctx: StageContext) -> bool:
    outs = ctx.outs
    if not (outs / "web_summary.html").is_file():
        return False
    if (outs / "web_summary.html").stat().st_size == 0:
        return False
    mat = outs / "filtered_feature_bc_matrix"
    for name in ("barcodes.tsv.gz", "features.tsv.gz", "matrix.mtx.gz"):
        f = mat / name
        if not f.is_file() or f.stat().st_size == 0:
            return False
    if ctx.cfg.get("pipeline", "create_bam", default=True) and \
            not (outs / "possorted_genome_bam.bam").is_file():
        return False
    return True


def stage_loom(ctx: StageContext):
    if not ctx.cfg.get("pipeline", "make_loom", default=True):
        return []
    loom = ctx.loom_dir / f"{ctx.sid}.loom"
    if loom.is_file() and _loom_ok(loom):
        ctx.log.info("loom already present — skipping")
        return [str(loom)]
    rm_rf(ctx.loom_dir)
    gtf = _resolve_gtf(ctx)
    ctx.runner.velocyto(ctx.sid, ctx.outs, gtf, ctx.loom_dir,
                        int(ctx.cfg.get("concurrency", "convert_threads",
                                        default=6)), ctx.log)
    if not _loom_ok(loom):
        raise ToolError(f"loom failed validation: {loom}")
    return [str(loom)]


def _loom_ok(loom: Path) -> bool:
    try:
        with open(loom, "rb") as f:
            return f.read(4) == b"\x89HDF" and loom.stat().st_size > 512
    except OSError:
        return False


def _resolve_gtf(ctx: StageContext) -> Path:
    genes = ctx.cfg.path("reference_dir") / "genes"
    plain = genes / "genes.gtf"
    if plain.is_file():
        return plain
    gz = genes / "genes.gtf.gz"
    if gz.is_file():
        cache = ctx.cfg.path("work_root") / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        ref_tag = ctx.cfg.path("reference_dir").name
        out = cache / f"{ref_tag}.genes.gtf"
        if not out.is_file() or out.stat().st_size == 0:
            ctx.log.info("decompressing GTF -> %s", out)
            tmp = out.with_suffix(".gtf.part")
            with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
            tmp.rename(out)
        return out
    raise ToolError(f"no genes.gtf(.gz) under {genes}")


def stage_archiving(ctx: StageContext):
    arch = ctx.archive
    outputs = []
    # 1) FASTQ zip (stored, since inputs are already gzipped) + manifest
    archives = arch / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    zpath = archives / f"{ctx.sid}.fastq.zip"
    manifest_path = archives / f"{ctx.sid}.fastq.manifest.json"
    fastqs = sorted(ctx.fastq_dir.glob("*.fastq.gz"))
    if not fastqs:
        raise ToolError(f"no FASTQs to archive for {ctx.sid}")
    manifest = {"sample": ctx.sid, "files": []}
    for f in fastqs:
        manifest["files"].append({"name": f.name, "size": f.stat().st_size,
                                  "sha256": sha256_file(f)})
    need_zip = True
    if zpath.is_file() and manifest_path.is_file():
        try:
            old = json.loads(manifest_path.read_text())
            if old.get("files") == manifest["files"] and zipfile.is_zipfile(zpath):
                need_zip = False
                ctx.log.info("fastq archive already present and matching")
        except (ValueError, OSError):
            pass
    if need_zip:
        tmp = zpath.with_suffix(".zip.part")
        rm_rf(tmp)
        ctx.log.info("writing %s (%d files)", zpath.name, len(fastqs))
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as z:
            for f in fastqs:
                z.write(f, arcname=f.name)
        tmp.rename(zpath)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    zip_sha = sha256_file(zpath)
    (archives / f"{ctx.sid}.fastq.zip.sha256").write_text(
        f"{zip_sha}  {zpath.name}\n")
    outputs += [str(zpath), str(manifest_path)]

    # 2) full Cell Ranger outs
    res_dst = arch / "results" / "outs"
    _copytree_resumable(ctx.outs, res_dst, ctx.log)
    outputs.append(str(res_dst / "web_summary.html"))

    # 3) loom
    loom_src = ctx.loom_dir / f"{ctx.sid}.loom"
    if loom_src.is_file():
        loom_dst_dir = arch / "loom"
        loom_dst_dir.mkdir(parents=True, exist_ok=True)
        loom_dst = loom_dst_dir / loom_src.name
        if not loom_dst.is_file() or loom_dst.stat().st_size != loom_src.stat().st_size:
            _copy_file_atomic(loom_src, loom_dst)
        outputs.append(str(loom_dst))

    # 4) logs + repro
    logs_dst = arch / "logs"
    logs_dst.mkdir(parents=True, exist_ok=True)
    log_src = ctx.cfg.logs_dir / f"{ctx.sid}.log"
    if log_src.is_file():
        shutil.copy2(log_src, logs_dst / log_src.name)
    read_decisions = ctx.sample["meta"].get("read_role_decisions", {})
    if read_decisions:
        (logs_dst / f"{ctx.sid}.read-role-decisions.json").write_text(
            json.dumps(read_decisions, indent=2))
    paired_decision = ctx.sample["meta"].get("paired_input_decision")
    if paired_decision:
        paired_path = logs_dst / f"{ctx.sid}.paired-input-decision.json"
        paired_path.write_text(json.dumps(paired_decision, indent=2))
        outputs.append(str(paired_path))
    repro_dir = arch / "repro"
    rec = repro.write_repro(ctx, repro_dir, zip_sha)
    outputs += [str(p) for p in rec]
    ctx.state.update_meta(ctx.sid, archive_dir=str(arch), fastq_zip_sha256=zip_sha)
    return outputs


def _copy_file_atomic(src: Path, dst: Path):
    tmp = dst.with_name(dst.name + ".part")
    shutil.copy2(src, tmp)
    tmp.rename(dst)


def _copytree_resumable(src: Path, dst: Path, log):
    """Copy tree, skipping files already present with the same size."""
    for root, _dirs, files in __import__("os").walk(src):
        rroot = Path(root).relative_to(src)
        (dst / rroot).mkdir(parents=True, exist_ok=True)
        for name in files:
            s = Path(root) / name
            d = dst / rroot / name
            if d.is_file() and d.stat().st_size == s.stat().st_size:
                continue
            _copy_file_atomic(s, d)


def stage_verifying(ctx: StageContext):
    """Checksum-verify archive copies before intermediates may be deleted."""
    arch = ctx.archive
    report = {"sample": ctx.sid, "verified_at": time.time(), "checks": []}
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        report["checks"].append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            ok = False

    zpath = arch / "archives" / f"{ctx.sid}.fastq.zip"
    sha_file = arch / "archives" / f"{ctx.sid}.fastq.zip.sha256"
    check("fastq_zip_exists", zpath.is_file(), str(zpath))
    if zpath.is_file() and sha_file.is_file():
        recorded = sha_file.read_text().split()[0]
        actual = sha256_file(zpath)
        check("fastq_zip_sha256", recorded == actual,
              f"recorded {recorded[:12]} vs actual {actual[:12]}")
        try:
            with zipfile.ZipFile(zpath) as z:
                bad = z.testzip()
                check("fastq_zip_integrity", bad is None, str(bad))
                manifest = json.loads(
                    (arch / "archives" / f"{ctx.sid}.fastq.manifest.json").read_text())
                names = set(z.namelist())
                missing = [f["name"] for f in manifest["files"]
                           if f["name"] not in names]
                check("fastq_zip_complete", not missing, ",".join(missing))
        except zipfile.BadZipFile as e:
            check("fastq_zip_integrity", False, f"unreadable zip: {e}")

    for rel in ("results/outs/web_summary.html",
                "results/outs/filtered_feature_bc_matrix/matrix.mtx.gz"):
        p = arch / rel
        check(f"archive:{rel}", p.is_file() and p.stat().st_size > 0, str(p))

    # matrix + loom checksums vs working copies
    src_mat = ctx.outs / "filtered_feature_bc_matrix" / "matrix.mtx.gz"
    dst_mat = arch / "results" / "outs" / "filtered_feature_bc_matrix" / "matrix.mtx.gz"
    if src_mat.is_file() and dst_mat.is_file():
        check("matrix_sha256", sha256_file(src_mat) == sha256_file(dst_mat))
    paired = ctx.sample["meta"].get("paired_input_decision")
    if paired:
        try:
            archived_decision = json.loads((arch / "logs" / f"{ctx.sid}.paired-input-decision.json").read_text())
            originals = json.loads((arch / "archives" / f"{ctx.sid}.fastq.manifest.json").read_text())
            original_files = {f["name"]: {"size": f["size"], "sha256": f["sha256"]}
                              for f in originals["files"]}
            check("paired_decision_archived", archived_decision == paired)
            check("paired_original_reads_preserved", paired["sources"] == original_files)
            check("paired_reproduction_helper", (arch / "repro" / "paired_input.py").is_file())
        except (OSError, ValueError, KeyError) as e:
            check("paired_original_reads_preserved", False, str(e))
    loom_src = ctx.loom_dir / f"{ctx.sid}.loom"
    loom_dst = arch / "loom" / f"{ctx.sid}.loom"
    if loom_src.is_file():
        check("loom_archived", loom_dst.is_file())
        if loom_dst.is_file():
            check("loom_sha256", sha256_file(loom_src) == sha256_file(loom_dst))

    ctx.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    rpt = ctx.cfg.reports_dir / f"{ctx.sid}.json"
    rpt.write_text(json.dumps(report, indent=1))
    # keep a copy with the archive
    (arch / "logs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(rpt, arch / "logs" / rpt.name)
    if not ok:
        failed = [c["name"] for c in report["checks"] if not c["ok"]]
        raise ToolError(f"archive verification failed: {', '.join(failed)}")
    return [str(rpt)]


def _remove_inbox_inputs(ctx: StageContext) -> None:
    """Remove configured source inputs after archive verification."""
    if not (ctx.cfg.get("cleanup", "remove_inbox", default=False)
            and ctx.sample["mode"] == "sra"):
        return
    for srr in ctx.sample["srrs"]:
        p = resolve_sra_inputs(ctx.cfg, srr)
        if p:
            ctx.log.info("removing inbox input %s", p)
            rm_rf(p)
            if p.exists():
                raise ToolError(f"cleanup could not remove inbox input {p}")


def finalize_deferred_cleanup(ctx: StageContext) -> None:
    """Remove a verified sample tree after Docker Desktop released its mounts.

    This runs from the scheduler only when there are no active workers or
    Docker containers.  Removing the complete tree also catches tool scratch
    files that a stage-specific list may not know about.
    """
    cleanup_storage = getattr(ctx.runner, "cleanup_sample_storage", None)
    if cleanup_storage:
        cleanup_storage(ctx.sid)
    rm_rf(ctx.work)
    if ctx.work.exists():
        raise ToolError(f"cleanup could not remove work directory {ctx.work}")
    _remove_inbox_inputs(ctx)
    ctx.state.update_meta(
        ctx.sid, cleanup_pending=False, cleanup_error="",
        cleanup_completed_at=time.time())
    ctx.log.info("deferred cleanup completed for %s", ctx.sid)


def stage_cleaning(ctx: StageContext):
    """Remove work only after archive verification and Docker mount release."""
    if ctx.runner.deferred_cleanup_requires_restart():
        ctx.state.update_meta(
            ctx.sid, cleanup_pending=True, cleanup_error="",
            cleanup_requested_at=time.time(), cleanup_docker_released_at=None)
        ctx.log.info(
            "working intermediates retained until the queue is Docker-idle; "
            "cleanup will restart Docker Desktop safely before deletion")
        return []

    cleanup_storage = getattr(ctx.runner, "cleanup_sample_storage", None)
    if cleanup_storage:
        cleanup_storage(ctx.sid)
    for sub in (ctx.sra_dir, ctx.fastq_dir, ctx.cr_dir, ctx.loom_dir,
                ctx.work / "raw_fastq", ctx.work / "fq_tmp"):
        rm_rf(sub)
        if sub.exists():
            raise ToolError(f"cleanup could not remove {sub}")
    _remove_inbox_inputs(ctx)
    ctx.state.update_meta(
        ctx.sid, cleanup_pending=False, cleanup_error="",
        cleanup_completed_at=time.time())
    ctx.log.info("cleaned working intermediates for %s", ctx.sid)
    return []


STAGE_FUNCS = {
    "downloading": stage_downloading,
    "converting": stage_converting,
    "cellranger": stage_cellranger,
    "loom": stage_loom,
    "archiving": stage_archiving,
    "verifying": stage_verifying,
    "cleaning": stage_cleaning,
}


def clean_partial(ctx: StageContext, stage: str) -> None:
    """Remove partial outputs of a failed stage so retry starts clean."""
    ctx.log.info("cleaning partial outputs of stage %r", stage)
    runner_cleanup = getattr(ctx.runner, "clean_partial", None)
    if runner_cleanup:
        runner_cleanup(ctx.sid, stage)
    rm_rf(ctx.sentinel(stage))
    if stage == "downloading":
        pass  # prefetch resumes safely; keep partial .sra handling to sra-tools
    elif stage == "converting":
        # Keep raw FASTQs that have an atomic fasterq success manifest. Each
        # lane revalidates the manifest and removes any untrusted partial data.
        rm_rf(ctx.work / "fq_tmp")
        # keep fully valid lanes; they are revalidated per-lane on rerun
    elif stage == "cellranger":
        rm_rf(ctx.cr_dir / ctx.sid)
    elif stage == "loom":
        rm_rf(ctx.loom_dir)
    elif stage in ("archiving", "verifying"):
        pass  # archive writes are resumable/atomic per file
    ctx.state.update_meta(ctx.sid)


def run_sample(ctx: StageContext, semaphores: dict, pause_check=None,
               stop_check=None, stage_names=None) -> None:
    """Run all stages for one sample, honoring sentinels and semaphores."""
    selected = list(stage_names) if stage_names is not None else STAGES
    unknown = [stage for stage in selected if stage not in STAGE_FUNCS]
    if unknown:
        raise ValueError(f"unknown pipeline stage(s): {', '.join(unknown)}")
    for stage in selected:
        ctx.refresh()
        if stop_check and stop_check():
            raise ShutdownError()
        if pause_check:
            pause_check()
        ensure_ready(ctx, stage)
        if ctx.sentinel_valid(stage):
            ctx.log.info("stage %s: sentinel valid — skipping", stage)
            continue
        sem = semaphores.get(stage)
        acquired = False
        try:
            if sem:
                acquired = sem.acquire(blocking=False)
                if not acquired:
                    ctx.state.upsert_sample(ctx.sid, status="waiting", stage=stage,
                                            error="")
                    ctx.log.info("waiting for %s slot", stage)
                    while not acquired:
                        if stop_check and stop_check():
                            raise ShutdownError()
                        if pause_check:
                            pause_check()
                        acquired = sem.acquire(timeout=1.0)
            # Pause/stop may have changed while this worker waited.
            if stop_check and stop_check():
                raise ShutdownError()
            if pause_check:
                pause_check()
            try:
                ctx.runner.assert_sample_idle(ctx.sid)
            except Exception as e:  # noqa: BLE001
                ctx.state.upsert_sample(ctx.sid, status="failed", stage=stage,
                                        error=str(e)[:1000])
                ctx.log.error("stage %s blocked by Docker state: %s", stage, e)
                raise

            ev = ctx.state.event_start(ctx.sid, stage)
            ctx.state.upsert_sample(ctx.sid, status="active", stage=stage, error="")
            ctx.log.info("=== stage %s started ===", stage)
            t0 = time.time()
            try:
                outputs = STAGE_FUNCS[stage](ctx)
                ctx.sentinel_write(stage, outputs or [])
                ctx.state.event_end(ev, "completed", 0)
                ctx.log.info("=== stage %s completed in %.1fs ===",
                             stage, time.time() - t0)
            except (PauseError, ShutdownError):
                ctx.state.event_end(ev, "interrupted", 130,
                                    "service paused or stopping")
                raise
            except Exception as e:  # noqa: BLE001
                if stop_check and stop_check():
                    ctx.state.event_end(
                        ev, "interrupted", 130,
                        "tool stopped by controlled service shutdown")
                    raise ShutdownError() from e
                ctx.state.event_end(ev, "failed", 1, str(e))
                ctx.state.upsert_sample(ctx.sid, status="failed", stage=stage,
                                        error=str(e)[:1000])
                ctx.log.error("stage %s FAILED: %s", stage, e)
                raise
        finally:
            if sem and acquired:
                sem.release()
    if selected == STAGES:
        ctx.state.upsert_sample(ctx.sid, status="done", stage="done", error="")
        ctx.log.info("sample %s DONE", ctx.sid)
