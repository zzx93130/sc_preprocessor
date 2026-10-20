"""Shared helpers: logging, hashing, filesystem checks."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def sample_logger(name: str, log_path: Path) -> logging.Logger:
    """Per-sample logger writing to its own file (and stderr)."""
    logger = logging.getLogger(f"scpre.sample.{name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path.parent.mkdir(parents=True, exist_ok=True)
    have = {getattr(h, "_scpre_path", None) for h in logger.handlers}
    if str(log_path) not in have:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        fh._scpre_path = str(log_path)  # type: ignore[attr-defined]
        logger.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in logger.handlers):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(asctime)s [" + name + "] %(message)s"))
        logger.addHandler(sh)
    return logger


def app_logger() -> logging.Logger:
    logger = logging.getLogger("scpre")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    return logger


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def free_bytes(path: Path) -> int:
    if hasattr(os, "statvfs"):
        st = os.statvfs(str(path))
        return st.f_bavail * st.f_frsize
    return shutil.disk_usage(path).free


def total_bytes(path: Path) -> int:
    if hasattr(os, "statvfs"):
        st = os.statvfs(str(path))
        return st.f_blocks * st.f_frsize
    return shutil.disk_usage(path).total


def disk_usage(path: Path) -> dict:
    try:
        if hasattr(os, "statvfs"):
            st = os.statvfs(str(path))
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            inodes_total = st.f_files
            inodes_free = st.f_favail
        else:
            st = shutil.disk_usage(path)
            total, free = st.total, st.free
            # Windows filesystems do not expose Unix inode accounting.
            inodes_total = inodes_free = 0
        return {"path": str(path), "total": total, "free": free,
                "used": total - free,
                "inodes_total": inodes_total,
                "inodes_free": inodes_free,
                "ok": True}
    except (OSError, ValueError) as e:
        return {"path": str(path), "total": 0, "free": 0, "used": 0,
                "inodes_total": 0, "inodes_free": 0,
                "ok": False, "error": str(e)}


def path_available(path: Path) -> bool:
    """True when the path exists and its filesystem responds (drive mounted)."""
    try:
        if hasattr(os, "statvfs"):
            os.statvfs(str(path))
        else:
            shutil.disk_usage(path)
        return path.exists()
    except (OSError, ValueError):
        return False


def writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".scpre_write_probe_{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def run_cmd(argv, logger=None, cwd=None, env=None, timeout=None) -> subprocess.CompletedProcess:
    """Run a command, streaming output lines to the logger."""
    if logger:
        logger.info("RUN: %s", " ".join(str(a) for a in argv))
    proc = subprocess.Popen(
        [str(a) for a in argv], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace",
    )
    lines = []
    start = time.time()
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        if logger:
            logger.info("| %s", line)
        if timeout and time.time() - start > timeout:
            proc.kill()
            raise subprocess.TimeoutExpired(argv, timeout)
    proc.wait()
    return subprocess.CompletedProcess(argv, proc.returncode, "\n".join(lines), "")


def rm_rf(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def stable_size(path: Path, wait_s: float = 2.0) -> bool:
    """True when a file's size and mtime are unchanged across wait_s."""
    try:
        st1 = path.stat()
        time.sleep(wait_s)
        st2 = path.stat()
        return st1.st_size == st2.st_size and st1.st_mtime == st2.st_mtime
    except OSError:
        return False


def gzip_ok(path: Path) -> bool:
    """Cheap integrity check: gzip magic + non-trivial size."""
    try:
        with open(path, "rb") as f:
            magic = f.read(2)
        return magic == b"\x1f\x8b" and path.stat().st_size > 20
    except OSError:
        return False
