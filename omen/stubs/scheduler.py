"""Scheduler stub.

Contract (Phase 5 owns the real implementation):
  schedule(name, when, payload) -> job
  cancel(job_id) -> bool
  list_jobs() -> list of jobs
Nothing is ever executed here: no timers, threads, or background work.
"""

from __future__ import annotations

import uuid
from typing import Any


class NoOpScheduler:
    phase = 5
    implementation = "stub-no-op"

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}

    def schedule(self, name: str, when: str, payload: dict | None = None) -> dict:
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "name": name, "when": when, "payload": dict(payload or {}), "status": "stubbed"}
        self._jobs[job_id] = job
        return job

    def cancel(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def list_jobs(self) -> list[dict]:
        return list(self._jobs.values())

    def status(self) -> dict:
        return {"phase": self.phase, "implementation": self.implementation, "jobs": len(self._jobs)}
