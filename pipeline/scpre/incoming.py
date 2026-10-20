"""Manager-owned SRA download, validation, and immutable ready store."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .util import sha256_file


class IncomingError(RuntimeError):
    pass


class IncomingStore:
    def __init__(self, shared_root: Path, prefetch: str = "prefetch",
                 validator: str = "vdb-validate"):
        self.root = shared_root / "incoming"
        self.partial = self.root / ".partial"
        self.ready = self.root / "ready"
        self.prefetch = prefetch
        self.validator = validator

    def locate(self, srr: str) -> Path | None:
        for candidate in (
            self.ready / srr / f"{srr}.sra",
            self.ready / f"{srr}.sra",
            self.ready / srr,
        ):
            if candidate.is_file():
                return candidate
        return None

    def record_for(self, srr: str) -> dict | None:
        path = self.locate(srr)
        if not path:
            return None
        meta = path.parent / f"{srr}.ready.json"
        if meta.is_file():
            try:
                record = json.loads(meta.read_text())
                if (record.get("size") == path.stat().st_size
                        and record.get("sha256")):
                    return record
            except (OSError, json.JSONDecodeError):
                pass
        record = {
            "srr": srr,
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "ready_at": time.time(),
        }
        meta.write_text(json.dumps(record, indent=2, sort_keys=True))
        return record

    def _validate_sra(self, path: Path) -> None:
        result = subprocess.run(
            [self.validator, str(path)], capture_output=True, text=True
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise IncomingError(f"vdb-validate failed for {path}: {detail}")

    def ingest(self, srr: str, source: Path, validate: bool = True) -> dict:
        if self.locate(srr):
            return self.record_for(srr) or {}
        if not source.is_file():
            raise IncomingError(f"input does not exist: {source}")
        self.partial.mkdir(parents=True, exist_ok=True)
        stage = self.partial / f"{srr}.{os.getpid()}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        staged = stage / f"{srr}.sra"
        shutil.copy2(source, staged)
        if validate:
            self._validate_sra(staged)
        digest = sha256_file(staged)
        final = self.ready / srr
        final.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "srr": srr,
            "path": str(final / f"{srr}.sra"),
            "size": staged.stat().st_size,
            "sha256": digest,
            "ready_at": time.time(),
        }
        (stage / f"{srr}.ready.json").write_text(
            json.dumps(record, indent=2, sort_keys=True)
        )
        os.replace(stage, final)
        return record

    def download(self, srr: str) -> dict:
        existing = self.record_for(srr)
        if existing:
            return existing
        self.partial.mkdir(parents=True, exist_ok=True)
        download_root = self.partial / f"{srr}.download"
        if download_root.exists():
            shutil.rmtree(download_root)
        result = subprocess.run(
            [self.prefetch, "--max-size", "u", "-O", str(download_root), srr],
            capture_output=True, text=True,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise IncomingError(f"prefetch failed for {srr}: {detail}")
        candidates = list(download_root.rglob(f"{srr}.sra"))
        if not candidates:
            raise IncomingError(f"prefetch completed but {srr}.sra is missing")
        return self.ingest(srr, candidates[0], validate=True)

    def ingest_fastq(self, sample: str, source: Path) -> dict:
        if not source.is_file():
            raise IncomingError(f"FASTQ input does not exist: {source}")
        final_dir = self.ready / "fastq" / sample
        final_dir.mkdir(parents=True, exist_ok=True)
        target = final_dir / source.name
        digest = sha256_file(source)
        if target.is_file() and sha256_file(target) == digest:
            return {
                "kind": "fastq", "name": source.name, "path": str(target),
                "size": target.stat().st_size, "sha256": digest,
            }
        part = target.with_suffix(target.suffix + f".{os.getpid()}.part")
        shutil.copy2(source, part)
        if sha256_file(part) != digest:
            part.unlink(missing_ok=True)
            raise IncomingError(
                f"FASTQ checksum changed while staging {source}"
            )
        os.replace(part, target)
        return {
            "kind": "fastq", "name": source.name, "path": str(target),
            "size": target.stat().st_size, "sha256": digest,
        }

    def remove_committed(self, srrs: list[str]) -> None:
        for srr in srrs:
            path = self.locate(srr)
            if not path:
                continue
            parent = path.parent
            if parent.name == srr and parent.parent == self.ready:
                shutil.rmtree(parent)
            else:
                path.unlink()
