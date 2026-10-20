"""Configuration: YAML load/validate + interactive first-run wizard.

No paths are hard-coded: everything lives in config.yaml at the project root
(the repo checkout). `scpre init` prompts for each value with a sensible
default and validates it before saving.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from .util import app_logger, free_bytes, human_bytes, writable_dir

log = app_logger()

CONFIG_NAME = "config.yaml"

DEFAULTS = {
    "project": "default",
    "paths": {
        "cellranger_dir": "",
        "reference_dir": "",
        "work_root": "",      # working SSD: all intermediates/temp
        "archive_root": "",   # Seagate: durable archive
        "inbox_dir": "",      # where .sra / fastq inputs arrive
    },
    "concurrency": {
        "max_active_samples": 2,   # samples in flight (stages overlap)
        "separate_downloads": False,  # independent download/preprocessing lanes
        "max_download_jobs": 1,
        "max_preprocessing_samples": 1,  # entire post-download lifecycle
        "download_ahead": 1,  # bound completed downloads waiting on preprocessing
        "max_cellranger_jobs": 1,  # never oversubscribe memory
        "cellranger_cores": "auto",
        "cellranger_mem_gb": "auto",
        "convert_threads": 6,
    },
    "docker": {
        "enabled": True,
        "cellranger_image": "scpre-crbase:latest",
        # Optional Docker named volume containing a Unix-native Cell Ranger
        # extraction.  This is needed on Windows when a bundle copied from
        # macOS contains AppleDouble XSym placeholders instead of symlinks.
        "cellranger_volume": "",
        # Optional prefix for per-sample Linux/ext4 pipestance volumes.  Keep
        # this set on Windows: Martian must create symlinks and cannot use a
        # Docker Desktop 9p bind mount as its working directory.
        "cellranger_work_volume_prefix": "",
        "cellranger_volume_min_free_gb": 100,
        "sratools_image": "scpre-sratools:latest",
        "velocyto_image": "scpre-velocyto:latest",
        "cellranger_platform": "linux/amd64",
        "sratools_platform": "linux/amd64",
        "velocyto_platform": "linux/amd64",
    },
    "space": {
        "fastq_multiplier": 2.2,       # fastq.gz footprint vs .sra size
        "conversion_multiplier": 10.0, # fasterq raw output + merge temp
        "cellranger_multiplier": 2.5,  # cellranger workdir vs fastq.gz
        "min_free_work_gb": 100,
        "min_free_archive_gb": 50,
    },
    "dashboard": {"host": "127.0.0.1", "port": 9123},
    "cleanup": {
        "remove_inbox": False,
        # macOS Docker Desktop/virtiofs can retain deleted bind-mounted files.
        # Restart only at a queue-idle point, before deleting completed work.
        "restart_docker_desktop_at_idle": True,
        "docker_desktop_restart_timeout_s": 180,
    },
    "pipeline": {
        "create_bam": True,           # required for velocyto
        "make_loom": True,
        "chemistry": "auto",
    },
    "metadata": {
        "sra_run_table": "",          # explicit path; else auto-discovered
        "read_maps": {},              # e.g. {"SRR123": "1=I1,2=R1,3=R2"}
    },
    "tools": {"mode": "docker"},      # docker | apptainer | mock
    "cluster": {
        "enabled": False,
        "shared_root": "/vol-rep1/scpre",
        "scratch_root": "/ssd/scpre",
        "shared_identity": "SCPRE_GLUSTER",
        "scratch_identity": "SCPRE_SCRATCH",
        "heartbeat_interval_s": 30,
        "heartbeat_stale_s": 180,
        "max_running_jobs": 2,
        "download_ahead": 2,
        "download_enabled": True,
        "remove_incoming_after_commit": False,
        "state_backup_keep": 30,
        "manager_min_free_gb": 20,
        "scratch_min_free_fraction": 0.15,
        "artifacts_catalog": "/vol-rep1/scpre/artifacts/catalog.json",
        "pbs": {
            "queue": "",
            "walltime": "48:00:00",
            "select": "select=1:ncpus=32:mem=128gb",
            "scratch_resource": "",
            "qsub": "qsub",
            "qstat": "qstat",
            "qdel": "qdel",
            "worker_script": "/opt/scpre/repo/deploy/pbs/scpre-worker.pbs",
            "python": "/opt/scpre/venv/bin/python",
        },
        "apptainer": {"executable": "apptainer"},
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict, root: Path):
        self.data = _deep_merge(DEFAULTS, data)
        self.root = root

    # --- accessors -------------------------------------------------------
    def __getitem__(self, key):
        return self.data[key]

    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def project(self) -> str:
        return self.data.get("project") or "default"

    def path(self, key: str) -> Path:
        return Path(self.data["paths"][key]).expanduser()

    @property
    def work_dir(self) -> Path:
        return self.path("work_root") / "work"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def logs_dir(self) -> Path:
        configured = self.get("paths", "logs_dir", default="")
        return Path(configured).expanduser() if configured else self.runs_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.runs_dir / "reports"

    @property
    def state_db(self) -> Path:
        configured = self.get("paths", "state_db", default="")
        return Path(configured).expanduser() if configured else self.runs_dir / "state.db"

    def archive_sample_dir(self, sample: str) -> Path:
        return self.path("archive_root") / self.project / sample

    def save(self) -> None:
        (self.root / CONFIG_NAME).write_text(
            yaml.safe_dump(self.data, sort_keys=False, default_flow_style=False))


def find_root(start: Path = None) -> Path:
    """Project root = nearest ancestor containing config.yaml or pipeline/."""
    cur = (start or Path.cwd()).resolve()
    for p in [cur] + list(cur.parents):
        if (p / CONFIG_NAME).exists() or (p / "pipeline" / "scpre").is_dir():
            return p
    return cur


def load(root: Path = None) -> Config:
    root = find_root(root)
    cfg_file = root / CONFIG_NAME
    if not cfg_file.exists():
        raise SystemExit(
            f"No {CONFIG_NAME} found at {root}. Run `scpre init` first.")
    with open(cfg_file) as f:
        data = yaml.safe_load(f) or {}
    return Config(data, root)


def load_file(path: Path, root: Path = None) -> Config:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"configuration file not found: {path}")
    with open(path) as handle:
        data = yaml.safe_load(handle) or {}
    project_root = find_root(root or Path.cwd())
    return Config(data, project_root)


# --- validation ----------------------------------------------------------

def validate_cellranger_dir(p: Path) -> str:
    if not p.is_dir():
        return f"{p} is not a directory"
    exe = p / "cellranger"
    binexe = p / "bin" / "cellranger"
    if not exe.exists() and not binexe.exists():
        return f"no `cellranger` executable found under {p}"
    return ""


def validate_reference_dir(p: Path) -> str:
    if not p.is_dir():
        return f"{p} is not a directory"
    if not (p / "reference.json").exists():
        return f"{p} has no reference.json (not a 10x reference?)"
    genes = p / "genes"
    if not genes.is_dir():
        return f"{p} has no genes/ directory (GTF needed for velocyto)"
    return ""


def validate_writable(p: Path) -> str:
    if not writable_dir(p):
        return f"{p} is not writable (drive unmounted or permissions issue)"
    return ""


def docker_probe(cfg: Config) -> str:
    """Check docker is reachable and cellranger runs; '' if OK."""
    docker = shutil.which("docker")
    if not docker:
        return "docker CLI not found on PATH"
    try:
        r = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return f"docker daemon not reachable: {r.stderr.strip()[:200]}"
    except Exception as e:  # noqa: BLE001
        return f"docker probe failed: {e}"
    cr = cfg.path("cellranger_dir")
    cr_source = cfg.get("docker", "cellranger_volume", default="") or str(cr)
    try:
        r = subprocess.run(
            [docker, "run", "--rm",
             "--platform", cfg.get("docker", "cellranger_platform"),
             "-v", f"{cr_source}:/opt/cellranger:ro",
             cfg.get("docker", "cellranger_image"),
             "/opt/cellranger/cellranger", "--version"],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return ("cellranger --version failed in container: "
                    + (r.stderr or r.stdout).strip()[:300])
        log.info("cellranger probe OK: %s", (r.stdout or "").strip())
    except Exception as e:  # noqa: BLE001
        return f"cellranger container probe failed: {e}"
    return ""


VALIDATORS = {
    "cellranger_dir": validate_cellranger_dir,
    "reference_dir": validate_reference_dir,
    "work_root": validate_writable,
    "archive_root": validate_writable,
    "inbox_dir": validate_writable,
}

PROMPTS = [
    ("cellranger_dir", "Cell Ranger installation directory"),
    ("reference_dir", "Reference transcriptome directory"),
    ("work_root", "Working SSD root for intermediates/temp"),
    ("archive_root", "Archive drive root for long-term storage"),
    ("inbox_dir", "Inbox directory for incoming .sra / FASTQ inputs"),
]


def _guess_defaults(root: Path) -> dict:
    """Best-effort defaults from the surrounding layout — offered, never forced."""
    g = {}
    base = root.parent  # e.g. /Volumes/SSD1_4T/sc_preprocessor
    tools = base / "tools"
    if tools.is_dir():
        crs = sorted(tools.glob("cellranger-*"))
        crs = [c for c in crs if c.is_dir()]
        if crs:
            g["cellranger_dir"] = str(crs[-1])
        refs = [r for r in tools.glob("refdata-*") if r.is_dir()]
        if refs:
            g["reference_dir"] = str(refs[0])
    g["work_root"] = str(base)
    if (base / "to_process").is_dir():
        g["inbox_dir"] = str(base / "to_process")
    for cand in ("/Volumes/Seagate Basic", "/Volumes/SeagateBasic"):
        if Path(cand).is_dir():
            g["archive_root"] = str(Path(cand) / "sc_archive")
            break
    return g


def init_wizard(root: Path, non_interactive: bool = False,
                overrides: dict = None, docker_check: bool = True) -> Config:
    cfg_file = root / CONFIG_NAME
    data = {}
    if cfg_file.exists():
        with open(cfg_file) as f:
            data = yaml.safe_load(f) or {}
    cfg = Config(data, root)
    guesses = _guess_defaults(root)
    overrides = overrides or {}

    print(f"\nscpre initial configuration — saved to {cfg_file}\n")
    for key, label in PROMPTS:
        current = overrides.get(key) or cfg.data["paths"].get(key) or guesses.get(key, "")
        while True:
            if non_interactive:
                value = current
            else:
                raw = input(f"{label}\n  [{current}]: ").strip()
                value = raw or current
            if not value:
                print("  ! a value is required")
                if non_interactive:
                    raise SystemExit(f"missing required path: {key}")
                continue
            err = VALIDATORS[key](Path(value).expanduser())
            if err:
                print(f"  ! {err}")
                if non_interactive:
                    raise SystemExit(f"invalid {key}: {err}")
                continue
            cfg.data["paths"][key] = str(Path(value).expanduser())
            break

    # concurrency
    ncpu = os.cpu_count() or 8
    default_cores = max(4, ncpu - 2)
    mem_gb = _detect_mem_gb()
    default_mem = max(16, int(mem_gb * 0.75)) if mem_gb else 32
    cc = cfg.data["concurrency"]
    if not non_interactive:
        raw = input(f"Max parallel samples in flight [{cc['max_active_samples']}]: ").strip()
        if raw:
            cc["max_active_samples"] = max(1, int(raw))
        raw = input(f"Max concurrent Cell Ranger jobs [{cc['max_cellranger_jobs']}]: ").strip()
        if raw:
            cc["max_cellranger_jobs"] = max(1, int(raw))
        raw = input(f"Cores per Cell Ranger job [{default_cores}]: ").strip()
        cc["cellranger_cores"] = int(raw) if raw else default_cores
        raw = input(f"Memory (GB) per Cell Ranger job [{default_mem}]: ").strip()
        cc["cellranger_mem_gb"] = int(raw) if raw else default_mem
    else:
        if cc["cellranger_cores"] == "auto":
            cc["cellranger_cores"] = default_cores
        if cc["cellranger_mem_gb"] == "auto":
            cc["cellranger_mem_gb"] = default_mem

    for k, v in overrides.items():
        if k in cfg.data["paths"]:
            cfg.data["paths"][k] = v

    # show free space so surprises surface now
    for key in ("work_root", "archive_root"):
        p = Path(cfg.data["paths"][key])
        print(f"  {key}: {human_bytes(free_bytes(p))} free")

    if docker_check and cfg.get("tools", "mode") == "docker":
        print("\nProbing Docker + Cell Ranger (may take a minute on first pull)...")
        err = docker_probe(cfg)
        if err:
            print(f"  ! WARNING: {err}")
            print("    Fix Docker Desktop (enable Rosetta x86_64 emulation, "
                  "allocate memory) before starting real runs.")
        else:
            print("  Docker + Cell Ranger probe OK")

    cfg.save()
    print(f"\nSaved {cfg_file}\n")
    return cfg


def _detect_mem_gb():
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True, timeout=5)
            return int(r.stdout.strip()) / 1e9
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except Exception:  # noqa: BLE001
        return None
