"""Versioned artifact catalog and checksum-verified node-local cache."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from .util import sha256_file


class ArtifactError(RuntimeError):
    pass


def hash_directory(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    total = 0
    count = 0
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = item.relative_to(path).as_posix().encode("utf-8")
        file_sha = sha256_file(item).encode("ascii")
        size = item.stat().st_size
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(size.to_bytes(8, "big"))
        digest.update(file_sha)
        total += size
        count += 1
    return digest.hexdigest(), total, count


def describe_artifact(name: str, source: Path) -> dict:
    source = source.resolve()
    if source.is_file():
        return {
            "name": name,
            "kind": "file",
            "path": str(source),
            "sha256": sha256_file(source),
            "size": source.stat().st_size,
            "files": 1,
        }
    if source.is_dir():
        digest, total, count = hash_directory(source)
        return {
            "name": name,
            "kind": "directory",
            "path": str(source),
            "sha256": digest,
            "size": total,
            "files": count,
        }
    raise ArtifactError(f"artifact does not exist: {source}")


def write_catalog(path: Path, artifacts: dict[str, Path]) -> dict:
    document = {
        "schema_version": 1,
        "created_at": time.time(),
        "artifacts": {
            name: describe_artifact(name, source)
            for name, source in sorted(artifacts.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    part.write_text(json.dumps(document, indent=2, sort_keys=True))
    os.replace(part, path)
    return document


def load_catalog(path: Path) -> dict:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read artifact catalog {path}: {exc}") from exc
    if document.get("schema_version") != 1:
        raise ArtifactError("unsupported artifact catalog schema")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ArtifactError("artifact catalog has no artifacts")
    for name, spec in artifacts.items():
        if spec.get("name") != name:
            raise ArtifactError(f"catalog name mismatch for {name}")
        if spec.get("kind") not in ("file", "directory"):
            raise ArtifactError(f"invalid artifact kind for {name}")
        if len(str(spec.get("sha256", ""))) != 64:
            raise ArtifactError(f"invalid artifact checksum for {name}")
    return document


def verify_artifact(spec: dict, path: Path) -> None:
    if spec["kind"] == "file":
        if not path.is_file() or path.stat().st_size != int(spec["size"]):
            raise ArtifactError(f"artifact file is missing or has wrong size: {path}")
        actual = sha256_file(path)
    else:
        if not path.is_dir():
            raise ArtifactError(f"artifact directory is missing: {path}")
        actual, total, count = hash_directory(path)
        if total != int(spec["size"]) or count != int(spec["files"]):
            raise ArtifactError(f"artifact directory inventory changed: {path}")
    if actual != spec["sha256"]:
        raise ArtifactError(f"artifact checksum mismatch for {spec['name']}")


class ArtifactCache:
    def __init__(self, cache_root: Path):
        self.cache_root = cache_root

    def materialize(self, spec: dict) -> Path:
        source = Path(spec["path"])
        target = self.cache_root / (
            f"{spec['name']}-{spec['sha256'][:12]}"
        )
        marker = target / ".scpre-artifact.json" if spec["kind"] == "directory" \
            else target.with_suffix(target.suffix + ".scpre-artifact.json")
        if target.exists() and marker.is_file():
            try:
                saved = json.loads(marker.read_text())
            except (OSError, json.JSONDecodeError):
                saved = {}
            if saved.get("sha256") == spec["sha256"]:
                return target
        self.cache_root.mkdir(parents=True, exist_ok=True)
        temp = self.cache_root / (
            f".{target.name}.{os.getpid()}.partial"
        )
        if temp.exists():
            if temp.is_dir():
                shutil.rmtree(temp)
            else:
                temp.unlink()
        if spec["kind"] == "directory":
            shutil.copytree(source, temp)
            verify_artifact(spec, temp)
            (temp / ".scpre-artifact.json").write_text(
                json.dumps(spec, indent=2, sort_keys=True)
            )
        else:
            shutil.copy2(source, temp)
            verify_artifact(spec, temp)
            temp_marker = temp.with_suffix(
                temp.suffix + ".scpre-artifact.json"
            )
            temp_marker.write_text(json.dumps(spec, indent=2, sort_keys=True))
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        os.replace(temp, target)
        if spec["kind"] == "file":
            os.replace(
                temp.with_suffix(temp.suffix + ".scpre-artifact.json"),
                marker,
            )
        return target
