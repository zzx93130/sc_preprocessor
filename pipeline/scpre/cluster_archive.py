"""Two-phase, checksum-verified sample archive commits on shared storage."""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from pathlib import Path

from .util import sha256_file


class ArchiveError(RuntimeError):
    pass


def _copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + f".{os.getpid()}.part")
    shutil.copy2(source, part)
    with open(part, "rb+") as handle:
        os.fsync(handle.fileno())
    os.replace(part, target)


def _add_sources(tar: tarfile.TarFile, sources: dict[str, Path]) -> None:
    for arcname, source in sorted(sources.items()):
        if source.exists():
            tar.add(source, arcname=arcname, recursive=True)


def build_bundle(path: Path, sources: dict[str, Path],
                 compression: str = "zstd") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    part.unlink(missing_ok=True)
    if compression == "zstd":
        try:
            import zstandard
        except ImportError as exc:
            raise ArchiveError(
                "zstandard is required to build cluster archives"
            ) from exc
        with open(part, "wb") as raw:
            compressor = zstandard.ZstdCompressor(level=6)
            with compressor.stream_writer(raw, closefd=False) as stream:
                with tarfile.open(fileobj=stream, mode="w|") as tar:
                    _add_sources(tar, sources)
            raw.flush()
            os.fsync(raw.fileno())
    elif compression == "gz":
        with tarfile.open(part, "w:gz") as tar:
            _add_sources(tar, sources)
    else:
        with tarfile.open(part, "w") as tar:
            _add_sources(tar, sources)
    os.replace(part, path)


def bundle_names(path: Path, compression: str) -> set[str]:
    names = set()
    if compression == "zstd":
        import zstandard
        with open(path, "rb") as raw:
            with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as tar:
                    for member in tar:
                        names.add(member.name)
    else:
        mode = "r:gz" if compression == "gz" else "r:"
        with tarfile.open(path, mode) as tar:
            names.update(tar.getnames())
    return names


class ArchiveCommitter:
    def __init__(self, archive_samples: Path, staging_root: Path):
        self.archive_samples = archive_samples
        self.staging_root = staging_root

    def commit(self, sample: str, project: str, attempt_id: str,
               manifest_sha256: str, local_bundle: Path,
               compression: str, summaries: dict[str, Path]) -> Path:
        final = self.archive_samples / project / sample
        committed = final / "COMMITTED.json"
        if committed.is_file():
            record = json.loads(committed.read_text())
            if record.get("attempt_id") == attempt_id:
                return final
            raise ArchiveError(
                f"sample {sample} already has a committed archive "
                f"from attempt {record.get('attempt_id')}"
            )
        staging = self.staging_root / project / sample / attempt_id
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        bundle_name = (
            "archive.tar.zst" if compression == "zstd"
            else "archive.tar.gz" if compression == "gz"
            else "archive.tar"
        )
        shared_bundle = staging / bundle_name
        _copy_file_atomic(local_bundle, shared_bundle)
        local_sha = sha256_file(local_bundle)
        shared_sha = sha256_file(shared_bundle)
        if local_sha != shared_sha:
            raise ArchiveError("archive checksum changed during shared copy")
        names = bundle_names(shared_bundle, compression)
        required = {"results/outs/web_summary.html", "fastq"}
        if not all(any(name == item or name.startswith(item + "/")
                       for name in names) for item in required):
            raise ArchiveError("archive bundle is missing required content")
        for relative, source in summaries.items():
            if source.is_dir():
                shutil.copytree(
                    source, staging / relative, dirs_exist_ok=True
                )
            elif source.is_file():
                _copy_file_atomic(source, staging / relative)
        (staging / f"{bundle_name}.sha256").write_text(
            f"{shared_sha}  {bundle_name}\n"
        )
        record = {
            "schema_version": 1,
            "sample": sample,
            "project": project,
            "attempt_id": attempt_id,
            "manifest_sha256": manifest_sha256,
            "archive": bundle_name,
            "archive_sha256": shared_sha,
            "archive_size": shared_bundle.stat().st_size,
            "committed_at": time.time(),
        }
        (staging / "manifest.json").write_text(
            json.dumps(record, indent=2, sort_keys=True)
        )
        (staging / "COMMITTED.json").write_text(
            json.dumps(record, indent=2, sort_keys=True)
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, final)
        except OSError as exc:
            if committed.is_file():
                existing = json.loads(committed.read_text())
                if existing.get("attempt_id") == attempt_id:
                    return final
            raise ArchiveError(f"atomic archive commit failed: {exc}") from exc
        return final
