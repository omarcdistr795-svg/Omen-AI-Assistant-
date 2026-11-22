"""MemoryStore stub.

Contract (Phase 2 owns the real implementation):
  remember(text, *, kind, metadata) -> record with id
  recall(query, *, limit) -> list of records
  forget(record_id) -> bool
All data is in-memory and lost on exit. No embeddings, no disk, no cloud.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


class InMemoryMemoryStore:
    phase = 2
    implementation = "stub-in-memory"

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}

    def remember(self, text: str, *, kind: str = "note", metadata: dict | None = None) -> dict:
        record_id = uuid.uuid4().hex
        record = {
            "id": record_id,
            "text": str(text),
            "kind": kind,
            "metadata": dict(metadata or {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._records[record_id] = record
        return record

    def recall(self, query: str = "", *, limit: int = 5) -> list[dict]:
        needle = str(query).lower()
        matches = [item for item in self._records.values() if not needle or needle in item["text"].lower()]
        return matches[:limit]

    def forget(self, record_id: str) -> bool:
        return self._records.pop(record_id, None) is not None

    def status(self) -> dict:
        return {"phase": self.phase, "implementation": self.implementation, "records": len(self._records)}
