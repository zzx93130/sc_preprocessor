"""Read-only inventory for migrating an existing single-machine queue."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .util import sha256_file


def _json(value, default):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _locate_input(inbox: Path, srr: str) -> Path | None:
    for candidate in (
        inbox / srr, inbox / f"{srr}.sra",
        inbox / srr / f"{srr}.sra",
    ):
        if candidate.is_file():
            return candidate
    return None


def build_plan(source_db: Path, source_archive: Path, source_inbox: Path,
               output: Path) -> dict:
    uri = source_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM samples ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    samples = []
    for row in rows:
        record = dict(row)
        sample_id = record["id"]
        project = record.get("project") or "default"
        srrs = _json(record.get("srrs"), [])
        meta = _json(record.get("meta"), {})
        archive = source_archive / project / sample_id
        required = [
            archive / "results" / "outs" / "web_summary.html",
            archive / "results" / "outs" / "metrics_summary.csv",
            archive / "results" / "outs" / "filtered_feature_bc_matrix"
            / "matrix.mtx.gz",
        ]
        archive_ok = all(path.is_file() and path.stat().st_size > 0
                         for path in required)
        inputs = []
        for srr in srrs:
            path = _locate_input(source_inbox, srr)
            inputs.append({
                "srr": srr,
                "path": str(path) if path else "",
                "present": bool(path),
                "size": path.stat().st_size if path else 0,
            })
        if record["status"] == "done" and archive_ok:
            action = "import_completed_archive"
            reason = "completed outputs passed the migration inventory"
        else:
            action = "requeue_from_verified_input"
            reason = (
                f"source queue status was {record['status']} at "
                f"stage {record['stage']}"
            )
        samples.append({
            "sample": sample_id,
            "project": project,
            "source_status": record["status"],
            "source_stage": record["stage"],
            "source_error": record.get("error") or "",
            "srrs": srrs,
            "inputs": inputs,
            "all_inputs_present": all(item["present"] for item in inputs),
            "source_archive": str(archive),
            "archive_ok": archive_ok,
            "archive_required_files": [str(path) for path in required],
            "loom_present": (
                archive / "loom" / f"{sample_id}.loom"
            ).is_file(),
            "action": action,
            "reason": reason,
            "metadata": {
                key: meta.get(key) for key in (
                    "gsm", "organism", "instrument", "library_strategy",
                    "read_counts", "cellranger_version",
                )
            },
        })
    document = {
        "schema_version": 1,
        "created_at": time.time(),
        "source_db": str(source_db.resolve()),
        "source_db_sha256": sha256_file(source_db),
        "source_archive": str(source_archive.resolve()),
        "source_inbox": str(source_inbox.resolve()),
        "samples": samples,
        "summary": {
            "total": len(samples),
            "completed_archives": sum(
                item["action"] == "import_completed_archive"
                for item in samples
            ),
            "requeue": sum(
                item["action"] == "requeue_from_verified_input"
                for item in samples
            ),
            "requeue_with_all_inputs": sum(
                item["action"] == "requeue_from_verified_input"
                and item["all_inputs_present"] for item in samples
            ),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_suffix(output.suffix + f".{os.getpid()}.part")
    part.write_text(json.dumps(document, indent=2, sort_keys=True))
    os.replace(part, output)
    return document
