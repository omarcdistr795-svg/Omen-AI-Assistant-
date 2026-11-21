"""Read-only view of Phase 3 local data that preserves ISO timestamps.

Phase 3's ``LocalDataStore.list`` applies full redaction, including its
phone-number pattern, which also matches ISO dates (``2026-11-21``). Deadline
arithmetic then becomes impossible. This view keeps every secret redaction rule
but disables only the email/phone patterns for machine-readable time fields.
It reads the same additive SQLite table; no earlier file is modified.
"""

from __future__ import annotations

from typing import Any

from ..safety.redaction import redact_text
from ..tools.coretools.localdata import LocalDataStore


class AutomationDataStore(LocalDataStore):
    def list(
        self,
        domain: str,
        *,
        status: str | None = None,
        query: str = "",
        limit: int = 50,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        domain = self._domain(domain)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM phase3_records WHERE domain=? ORDER BY updated_at DESC", (domain,)
            ).fetchall()
        results = [
            self._row(row)
            for row in rows
            if row["status"] != "deleted" and (status is None or row["status"] == status)
        ]
        if query:
            needle = query.lower()
            results = [item for item in results if needle in str(item).lower()]
        if start:
            results = [item for item in results if str(item.get("start") or item.get("due_at") or "") >= start]
        if end:
            results = [item for item in results if str(item.get("start") or item.get("due_at") or "") <= end]
        return self._clean(results[: max(1, min(int(limit), 200))])

    TIME_KEYS = frozenset({"start", "end", "due_at", "deadline", "created_at", "updated_at", "at"})

    @classmethod
    def _clean(cls, value: Any, *, key: str | None = None) -> Any:
        """Fully redact text, except known machine-readable timestamp fields."""
        if isinstance(value, str):
            if key in cls.TIME_KEYS:
                return value  # ISO timestamps, never user prose
            return redact_text(value)
        if isinstance(value, dict):
            return {item_key: cls._clean(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._clean(item) for item in value]
        return value
