"""Cluster mount identity checks and durable worker heartbeats."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .util import disk_usage


class MountError(RuntimeError):
    pass


def verify_mount(path: Path, expected_identity: str,
                 identity_name: str = ".scpre-volume-id",
                 require_mountpoint: bool = True) -> None:
    if not path.is_dir():
        raise MountError(f"required filesystem is unavailable: {path}")
    if require_mountpoint and not os.path.ismount(path):
        raise MountError(f"required path is not a mountpoint: {path}")
    identity = path / identity_name
    try:
        actual = identity.read_text().strip()
    except OSError as exc:
        raise MountError(f"volume identity file is unavailable: {identity}") from exc
    if actual != expected_identity:
        raise MountError(
            f"volume identity mismatch at {path}: expected "
            f"{expected_identity!r}, got {actual!r}"
        )


def write_json_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    part.write_text(json.dumps(document, indent=2, sort_keys=True))
    os.replace(part, path)


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def heartbeat_age(document: dict | None, now: float | None = None) -> float | None:
    if not document:
        return None
    try:
        return max(0.0, (now or time.time()) - float(document["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return None


class HeartbeatWriter(threading.Thread):
    def __init__(self, path: Path, provider, interval: float = 30.0):
        super().__init__(name="scpre-heartbeat", daemon=True)
        self.path = path
        self.provider = provider
        self.interval = max(1.0, float(interval))
        self._stop_event = threading.Event()

    def emit(self) -> None:
        document = dict(self.provider())
        document["updated_at"] = time.time()
        scratch_path = document.get("scratch_path")
        if scratch_path:
            usage = disk_usage(Path(scratch_path))
            document["scratch_free_bytes"] = usage.get("free", 0)
        write_json_atomic(self.path, document)

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.emit()
            except OSError:
                pass
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()
