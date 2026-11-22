"""Small, additive JSON state helpers for Phase 5 automation.

State is written atomically under ``state/automation/`` and can always be rolled
back by deleting that directory (no earlier phase reads it). Every write is
redacted, and no secret is ever accepted from config in this layer.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..safety.redaction import redact_value

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def parse_moment(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Parse an ISO timestamp, an HH:MM daily time, or a relative second count.

    Naive datetimes are treated as UTC (the configured system timezone). HH:MM
    resolves to the next occurrence at or after ``now``.
    """
    reference = now or utcnow()
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return reference + timedelta(seconds=float(value))
    text = str(value).strip()
    if not text:
        return None
    if ":" in text and len(text) <= 5:
        try:
            hour, minute = (int(part) for part in text.split(":", 1))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
        except ValueError:
            return None
        candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= reference:
            candidate += timedelta(days=1)
        return candidate
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            seconds = float(text)
        except ValueError:
            return None
        return reference + timedelta(seconds=seconds)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fingerprint(*parts: Any) -> str:
    body = json.dumps(list(parts), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


class JsonState:
    """One atomic, redacted JSON file. Missing/corrupt state fails loudly."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read automation state at {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Automation state must be a JSON object: {self.path}")
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(redact_value(data), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
            self.path.parent.chmod(0o700)
        except OSError as exc:  # fail loudly if permissions cannot be restricted
            raise RuntimeError(f"Cannot restrict automation state permissions: {exc}") from exc
