"""Immutable, checksummed work manifests exchanged by manager and workers."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

SCHEMA_VERSION = 1
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ManifestError(RuntimeError):
    pass


def canonical_bytes(document: dict) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def checksum(document: dict) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def validate(document: dict) -> None:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("unsupported worker manifest schema")
    for key in ("attempt_id", "sample", "project"):
        value = document.get(key)
        if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
            raise ManifestError(f"invalid {key}: {value!r}")
    inputs = document.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ManifestError("manifest has no inputs")
    for item in inputs:
        if not isinstance(item, dict):
            raise ManifestError("invalid input entry")
        kind = item.get("kind", "sra")
        if kind == "sra":
            if not SAFE_ID.fullmatch(str(item.get("srr", ""))):
                raise ManifestError("invalid input accession")
        elif kind == "fastq":
            name = str(item.get("name", ""))
            if Path(name).name != name or not SAFE_ID.fullmatch(name):
                raise ManifestError("invalid FASTQ input name")
        else:
            raise ManifestError(f"invalid input kind: {kind!r}")
        digest = str(item.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestError("input checksum is missing or invalid")
        if not Path(str(item.get("path", ""))).is_absolute():
            raise ManifestError("input path must be absolute")
    for key in ("paths", "resources", "artifacts", "pipeline"):
        if not isinstance(document.get(key), dict):
            raise ManifestError(f"manifest {key} must be an object")
    paths = document["paths"]
    for key in ("shared_root", "scratch_root", "archive_root"):
        if not Path(str(paths.get(key, ""))).is_absolute():
            raise ManifestError(f"manifest path {key} must be absolute")


def write_manifest(path: Path, document: dict) -> str:
    validate(document)
    payload = canonical_bytes(document)
    digest = hashlib.sha256(payload).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if path.exists():
        if path.read_bytes() != payload:
            raise ManifestError(f"refusing to replace immutable manifest {path}")
        if sidecar.exists() and sidecar.read_text().split()[0] != digest:
            raise ManifestError(f"manifest checksum sidecar disagrees for {path}")
        return digest
    part = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    part.write_bytes(payload)
    os.replace(part, path)
    side_part = sidecar.with_suffix(sidecar.suffix + f".{os.getpid()}.part")
    side_part.write_text(f"{digest}  {path.name}\n")
    os.replace(side_part, sidecar)
    try:
        path.chmod(0o444)
        sidecar.chmod(0o444)
    except OSError:
        pass
    return digest


def load_verified(path: Path, expected_sha256: str = "") -> tuple[dict, str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    actual = hashlib.sha256(payload).hexdigest()
    expected = expected_sha256.strip()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not expected and sidecar.is_file():
        expected = sidecar.read_text().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ManifestError("trusted manifest checksum was not provided")
    if actual != expected:
        raise ManifestError(
            f"manifest checksum mismatch: expected {expected}, got {actual}"
        )
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid manifest JSON: {exc}") from exc
    validate(document)
    return document, actual
