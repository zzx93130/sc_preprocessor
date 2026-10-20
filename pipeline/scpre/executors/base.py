"""Scheduler executor interface."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    state: str
    node: str = ""
    exit_code: int | None = None
    raw_state: str = ""


class ExecutorError(RuntimeError):
    pass
