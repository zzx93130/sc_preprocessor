"""SQLite-backed job state + metrics history. Survives restarts; WAL mode."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id TEXT PRIMARY KEY,
    project TEXT,
    mode TEXT,                 -- sra | fastq
    srrs TEXT,                 -- JSON list of run accessions (sra mode)
    status TEXT,               -- queued|active|paused|failed|done
    stage TEXT,                -- current/last stage name
    error TEXT,
    meta TEXT,                 -- JSON metadata (organism, read counts, ...)
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS stage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT,
    stage TEXT,
    status TEXT,               -- started|completed|failed|skipped
    started_at REAL,
    ended_at REAL,
    exit_code INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_sample ON stage_events(sample_id, id);
CREATE TABLE IF NOT EXISTS metrics (
    ts REAL,
    cpu_pct REAL,
    mem_used INTEGER,
    mem_total INTEGER,
    disks TEXT,                -- JSON
    jobs TEXT                  -- JSON per-job cpu/mem
);
CREATE INDEX IF NOT EXISTS ix_metrics_ts ON metrics(ts);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS cluster_attempts (
    attempt_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    status TEXT NOT NULL,
    pbs_job_id TEXT,
    node TEXT,
    preferred_node TEXT,
    pbs_state TEXT,
    manifest_path TEXT,
    manifest_sha256 TEXT,
    scratch_bytes INTEGER DEFAULT 0,
    scratch_path TEXT,
    exit_code INTEGER,
    error TEXT,
    created_at REAL,
    updated_at REAL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS ix_cluster_attempt_sample
    ON cluster_attempts(sample_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cluster_attempt_active_sample
    ON cluster_attempts(sample_id)
    WHERE status IN ('preparing','submitted','queued','running','archiving');
"""

ACTIVE_ATTEMPT_STATUSES = {
    "preparing", "submitted", "queued", "running", "archiving"
}


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # --- samples ---------------------------------------------------------
    def upsert_sample(self, sample_id: str, **fields) -> None:
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT id FROM samples WHERE id=?", (sample_id,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO samples (id, project, mode, srrs, status, stage,"
                    " error, meta, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sample_id,
                     fields.get("project", ""),
                     fields.get("mode", "sra"),
                     json.dumps(fields.get("srrs", [])),
                     fields.get("status", "queued"),
                     fields.get("stage", "queued"),
                     fields.get("error", ""),
                     json.dumps(fields.get("meta", {})),
                     now, now))
            else:
                sets, vals = [], []
                for k in ("project", "mode", "status", "stage", "error"):
                    if k in fields:
                        sets.append(f"{k}=?")
                        vals.append(fields[k])
                if "srrs" in fields:
                    sets.append("srrs=?")
                    vals.append(json.dumps(fields["srrs"]))
                if "meta" in fields:
                    sets.append("meta=?")
                    vals.append(json.dumps(fields["meta"]))
                sets.append("updated_at=?")
                vals.append(now)
                vals.append(sample_id)
                c.execute(f"UPDATE samples SET {', '.join(sets)} WHERE id=?", vals)

    def update_meta(self, sample_id: str, **kv) -> None:
        s = self.get_sample(sample_id)
        if not s:
            return
        meta = s["meta"]
        meta.update(kv)
        self.upsert_sample(sample_id, meta=meta)

    def get_sample(self, sample_id: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
        return self._sample_dict(row) if row else None

    def list_samples(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM samples ORDER BY created_at").fetchall()
        return [self._sample_dict(r) for r in rows]

    @staticmethod
    def _sample_dict(row) -> dict:
        d = dict(row)
        d["srrs"] = json.loads(d.get("srrs") or "[]")
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    # --- stage events ----------------------------------------------------
    def event_start(self, sample_id: str, stage: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO stage_events (sample_id, stage, status, started_at)"
                " VALUES (?,?,?,?)", (sample_id, stage, "started", time.time()))
            return cur.lastrowid

    def event_end(self, event_id: int, status: str, exit_code: int = 0,
                  detail: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE stage_events SET status=?, ended_at=?, exit_code=?, detail=?"
                " WHERE id=?", (status, time.time(), exit_code, detail[:2000], event_id))

    def interrupt_open_events(self, sample_id: str, detail: str) -> int:
        """Close stage events abandoned by a stopped service.

        Leaving them in ``started`` state makes the dashboard show a stage as
        running forever and hides the distinction between historical work and
        the new attempt created after recovery.
        """
        with self._conn() as c:
            cur = c.execute(
                "UPDATE stage_events SET status='interrupted', ended_at=?,"
                " exit_code=130, detail=?"
                " WHERE sample_id=? AND status='started' AND ended_at IS NULL",
                (time.time(), detail[:2000], sample_id))
            return cur.rowcount

    def events_for(self, sample_id: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM stage_events WHERE sample_id=? ORDER BY id",
                (sample_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- metrics ---------------------------------------------------------
    def metrics_insert(self, cpu_pct, mem_used, mem_total, disks: dict, jobs: dict):
        with self._conn() as c:
            c.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?)",
                      (time.time(), cpu_pct, mem_used, mem_total,
                       json.dumps(disks), json.dumps(jobs)))

    def metrics_recent(self, seconds: float = 3600):
        cutoff = time.time() - seconds
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM metrics WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["disks"] = json.loads(d["disks"] or "{}")
            d["jobs"] = json.loads(d["jobs"] or "{}")
            out.append(d)
        return out

    def metrics_prune(self, keep_seconds: float = 7 * 86400):
        with self._conn() as c:
            c.execute("DELETE FROM metrics WHERE ts < ?", (time.time() - keep_seconds,))

    def backup_to(self, path: Path) -> None:
        """Create an atomic, consistent SQLite backup."""
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        part.unlink(missing_ok=True)
        destination = sqlite3.connect(str(part))
        try:
            self._conn().backup(destination)
            check = destination.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError("backup integrity_check failed")
            destination.commit()
        finally:
            destination.close()
        os.replace(part, path)

    # --- cluster attempts -----------------------------------------------
    def create_attempt(self, sample_id: str, attempt_id: str, **fields) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO cluster_attempts (attempt_id, sample_id, status,"
                " pbs_job_id, node, preferred_node, pbs_state, manifest_path,"
                " manifest_sha256, scratch_bytes, scratch_path, exit_code,"
                " error, created_at, updated_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, sample_id, fields.get("status", "preparing"),
                 fields.get("pbs_job_id", ""), fields.get("node", ""),
                 fields.get("preferred_node", ""), fields.get("pbs_state", ""),
                 fields.get("manifest_path", ""),
                 fields.get("manifest_sha256", ""),
                 int(fields.get("scratch_bytes", 0)),
                 fields.get("scratch_path", ""), fields.get("exit_code"),
                 fields.get("error", ""), now, now, fields.get("ended_at")))

    def update_attempt(self, attempt_id: str, **fields) -> None:
        allowed = {
            "status", "pbs_job_id", "node", "preferred_node", "pbs_state",
            "manifest_path", "manifest_sha256", "scratch_bytes",
            "scratch_path", "exit_code", "error", "ended_at",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unsupported attempt field: {key}")
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        values.extend([time.time(), attempt_id])
        with self._conn() as c:
            c.execute(
                f"UPDATE cluster_attempts SET {', '.join(sets)} WHERE attempt_id=?",
                values)

    def get_attempt(self, attempt_id: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM cluster_attempts WHERE attempt_id=?",
                (attempt_id,)).fetchone()
        return dict(row) if row else None

    def attempts_for(self, sample_id: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM cluster_attempts WHERE sample_id=?"
                " ORDER BY created_at", (sample_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_active_attempts(self):
        marks = ",".join("?" for _ in ACTIVE_ATTEMPT_STATUSES)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM cluster_attempts WHERE status IN ({marks})"
                " ORDER BY created_at", tuple(sorted(ACTIVE_ATTEMPT_STATUSES))
            ).fetchall()
        return [dict(row) for row in rows]

    def active_attempt_for(self, sample_id: str):
        marks = ",".join("?" for _ in ACTIVE_ATTEMPT_STATUSES)
        args = [sample_id] + sorted(ACTIVE_ATTEMPT_STATUSES)
        with self._conn() as c:
            row = c.execute(
                f"SELECT * FROM cluster_attempts WHERE sample_id=?"
                f" AND status IN ({marks}) ORDER BY created_at DESC LIMIT 1",
                args).fetchone()
        return dict(row) if row else None

    def last_attempt_for(self, sample_id: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM cluster_attempts WHERE sample_id=?"
                " ORDER BY created_at DESC LIMIT 1", (sample_id,)).fetchone()
        return dict(row) if row else None

    # --- kv --------------------------------------------------------------
    def kv_set(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv (key, value) VALUES (?,?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    def kv_get(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
