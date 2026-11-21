"""Append-only, hash-chained, redacted-at-write-time audit log."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..safety.redaction import redact_value

GENESIS = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class AuditError(RuntimeError):
    """Raised on audit-chain or access errors."""


class AuditLog:
    """Structured audit trail. Lines are only ever appended, never rewritten."""

    def __init__(
        self,
        path: Path | str,
        *,
        trace_id: str | None = None,
        user: str = "soham.bhagat",
        agent: str = "omen-phase1",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.access_path = self.path.parent / "audit_access.jsonl"
        self.trace_id = trace_id or uuid.uuid4().hex
        self.user = user
        self.agent = agent
        self._last_hash = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = json.loads(line).get("hash", GENESIS)
        return last

    def _secure(self, path: Path) -> None:
        try:
            path.chmod(0o600)
            path.parent.chmod(0o700)
        except OSError as exc:  # fail loudly on unexpected filesystems
            raise AuditError(f"Cannot restrict audit log permissions: {exc}") from exc

    def _hash(self, record: dict) -> str:
        body = {key: value for key, value in record.items() if key not in ("hash",)}
        return hashlib.sha256(self._last_hash.encode("utf-8") + _canonical(body)).hexdigest()

    def append(self, event: dict) -> dict:
        """Append one redacted event and return the stored record."""
        record: dict[str, Any] = {
            "timestamp": event.get("timestamp") or _utc_now(),
            "event_id": event.get("event_id") or uuid.uuid4().hex,
            "trace_id": event.get("trace_id") or self.trace_id,
            "session_id": event.get("session_id") or self.trace_id,
            "user": event.get("user") or self.user,
            "agent": event.get("agent") or self.agent,
            "model": event.get("model"),
            "provider": event.get("provider"),
            "tool": event.get("tool"),
            "target": redact_value(event.get("target")),
            "args": redact_value(event.get("args") or event.get("params") or {}),
            "decision": event.get("decision"),
            "approval": redact_value(event.get("approval") or {}),
            "result": redact_value(event.get("result")),
            "status": event.get("status"),
            "duration_ms": event.get("duration_ms"),
            "retries": event.get("retries", 0),
            "error": redact_value(event.get("error")),
            "tokens": event.get("tokens") or {},
            "cost_usd": event.get("cost_usd", 0.0),
            "resources": event.get("resources") or {},
            "summary": redact_value(event.get("summary") or ""),
            "event_type": event.get("event_type", "tool_call"),
            "prev_hash": self._last_hash,
        }
        record["hash"] = self._hash(record)
        line = _canonical(record).decode("utf-8") + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self._secure(self.path)
        self._last_hash = record["hash"]
        return record

    def _read_records(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise AuditError(f"Malformed audit line {line_no}: {exc}") from exc
        return records

    def search(self, *, actor: str | None = None, **filters: Any) -> list[dict]:
        """Search stored events. Equality, __contains, since/until and limit are supported."""
        records = self._read_records()
        if actor is None:
            actor = self.user
        limit = filters.pop("limit", None)
        results = []
        for record in records:
            matched = True
            for key, expected in filters.items():
                if key == "since":
                    matched &= str(record.get("timestamp", "")) >= str(expected)
                elif key == "until":
                    matched &= str(record.get("timestamp", "")) <= str(expected)
                elif key.endswith("__contains"):
                    field = key[: -len("__contains")]
                    matched &= str(expected) in json.dumps(record.get(field), ensure_ascii=False)
                else:
                    matched &= record.get(key) == expected
            if matched:
                results.append(record)
        if limit:
            results = results[-int(limit):]
        self._log_access("search", {"filters": redact_value(filters), "actor": actor, "count": len(results)})
        return results

    def verify_integrity(self) -> bool:
        records = self._read_records()
        previous = GENESIS
        for record in records:
            body = {key: value for key, value in record.items() if key != "hash"}
            expected = hashlib.sha256(previous.encode("utf-8") + _canonical(body)).hexdigest()
            if record.get("prev_hash") != previous or record.get("hash") != expected:
                return False
            previous = record["hash"]
        self._log_access("verify_integrity", {"actor": self.user, "count": len(records)})
        return True

    def _log_access(self, action: str, detail: dict) -> None:
        entry = {"timestamp": _utc_now(), "action": action, **redact_value(detail)}
        with self.access_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(_canonical(entry).decode("utf-8") + "\n")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self._secure(self.access_path)


def detect_alerts(
    events: list[dict],
    *,
    failure_threshold: int = 3,
    volume_threshold: int = 20,
    allowlist: tuple[str, ...] = (),
) -> list[dict]:
    """Detect repeated failures, first sensitive use, unusual tools, and volume spikes."""
    alerts: list[dict] = []
    failures: dict[str, int] = {}
    sensitive_seen: set[str] = set()
    for event in events:
        tool = str(event.get("tool") or "unknown")
        if str(event.get("status")) in ("error", "blocked", "denied", "failed"):
            failures[tool] = failures.get(tool, 0) + 1
        decision = str(event.get("decision") or "")
        if decision in ("escalated", "pending_approval") and tool not in sensitive_seen:
            sensitive_seen.add(tool)
            alerts.append({"type": "first_use_sensitive_tool", "tool": tool})
        if allowlist and tool not in allowlist:
            alerts.append({"type": "unusual_tool_usage", "tool": tool})
    for tool, count in failures.items():
        if count >= failure_threshold:
            alerts.append({"type": "repeated_failures", "tool": tool, "count": count})
    timestamps = sorted(str(event.get("timestamp") or "") for event in events)
    window: list[str] = []
    for stamp in timestamps:
        window.append(stamp)
        # events are ISO-8601 strings; a simple count-based window is sufficient
        # for Phase 1 anomaly signalling and needs no clock injection.
        if len(window) > volume_threshold:
            alerts.append({"type": "abnormal_volume", "count": len(window)})
            break
    return alerts
