"""Build and deployment alerts: CI failures, regressions and long jobs.

Events come from the append-only audit log or from an explicit ``ingest`` call
(a local adapter can feed CI/deployment hooks). Nothing here opens a network
connection; an external connector is BLOCKED-EXTERNAL until supplied by the
user and enabled in their private config.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .state import JsonState, fingerprint, iso, utcnow

FAILED = {"failed", "failure", "error", "blocked"}


class BuildAlerts:
    def __init__(
        self,
        config: Any,
        *,
        root: Path | str,
        notifier: Any,
        audit: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.audit = audit
        self._clock = clock or utcnow
        settings = config.get("automation.build_alerts") or {}
        self.enabled = bool(settings.get("enabled", True))
        self.long_job_ms = max(1, int(settings.get("long_job_ms", 300_000)))
        self.regression_window = max(1, int(settings.get("regression_window", 5)))
        state_dir = Path(root).resolve() / str(config.get("automation.state_dir", "state/automation"))
        self.state = JsonState(state_path or state_dir / "build_alerts.json")
        stored = self.state.load()
        self.alerts: list[dict] = stored.get("alerts") or []
        self._emitted: set[str] = set(stored.get("emitted") or [])

    def ingest(self, event: dict, *, now: datetime | None = None) -> dict | None:
        """Classify one external build/deployment event and notify if needed."""
        if not self.enabled:
            return None
        moment = now or self._clock()
        failure = str(event.get("status") or "").lower() in FAILED
        source = str(event.get("source") or event.get("tool") or "").lower()
        duration = float(event.get("duration_ms") or 0)
        alert: dict | None = None
        if failure and ("ci" in source or "build" in source):
            alert = self._make("ci_failure", "high", event, f"CI job {event.get('job') or event.get('tool') or 'unknown'} failed")
        elif failure and "deploy" in source:
            alert = self._make("deployment_failure", "critical", event, f"Deployment {event.get('job') or event.get('target') or 'unknown'} failed")
        elif failure and ("test" in source or "tests.run" in source):
            alert = self._make("test_failure", "high", event, f"Tests failed for {event.get('repo') or event.get('target') or 'local'}")
        elif duration >= self.long_job_ms:
            alert = self._make("long_running_job", "normal", event, f"Long-running job {event.get('job') or event.get('tool') or 'unknown'} took {int(duration)} ms")
        if alert is None:
            return None
        self._emit(alert, moment)
        return alert

    def scan(self, *, events: list[dict] | None = None, now: datetime | None = None) -> dict:
        """Scan audit events for test regressions and long-running jobs."""
        moment = now or self._clock()
        if not self.enabled:
            return {"status": "disabled", "alerts": []}
        records = events
        if records is None:
            records = self.audit.search(limit=200) if self.audit is not None else []
        records = sorted(records, key=lambda item: str(item.get("timestamp") or ""))
        recent: dict[str, list[str]] = {}
        found: list[dict] = []
        for record in records:
            if str(record.get("event_type")) != "tool_call":
                continue
            tool = str(record.get("tool") or "unknown")
            status = str(record.get("status") or "")
            command = str((record.get("args") or {}).get("command") or record.get("target") or tool)
            history = recent.setdefault(command, [])
            if status in FAILED:
                if "ok" in history[-self.regression_window:]:
                    found.append(self._make(
                        "test_regression", "high",
                        {"tool": tool, "command": command, "timestamp": record.get("timestamp")},
                        f"Test regression: {command} failed after previously passing",
                    ))
            history.append(status)
            duration = float(record.get("duration_ms") or 0)
            if duration >= self.long_job_ms:
                found.append(self._make(
                    "long_running_job", "normal",
                    {"tool": tool, "command": command, "duration_ms": duration, "timestamp": record.get("timestamp")},
                    f"Long-running job {command} took {int(duration)} ms",
                ))
        emitted = 0
        for alert in found:
            if alert["fingerprint"] in self._emitted:
                continue
            self._emit(alert, moment)
            emitted += 1
        return {"status": "ok", "scanned": len(records), "alerts_found": len(found), "emitted": emitted}

    def summary(self) -> list[dict]:
        return [dict(alert) for alert in self.alerts[-10:]]

    def status(self) -> dict:
        return {"enabled": self.enabled, "alerts": len(self.alerts), "long_job_ms": self.long_job_ms}

    # ----- internals -----------------------------------------------------
    @staticmethod
    def _make(kind: str, priority: str, event: dict, message: str) -> dict:
        key = fingerprint(kind, event.get("source") or event.get("tool"), event.get("job") or event.get("command") or event.get("target") or message)
        return {
            "type": kind,
            "priority": priority,
            "message": message,
            "fingerprint": key,
            "source": str(event.get("source") or event.get("tool") or "audit"),
            "detail": {name: event[name] for name in ("job", "command", "repo", "target", "duration_ms") if name in event},
        }

    def _emit(self, alert: dict, now: datetime) -> None:
        self._emitted.add(alert["fingerprint"])
        self.alerts.append({**alert, "at": iso(now)})
        self.alerts = self.alerts[-100:]
        self.state.save({"alerts": self.alerts, "emitted": sorted(self._emitted)})
        self.notifier.notify(
            alert["message"],
            level=alert["priority"],
            channel="auto",
            priority=alert["priority"],
            key=f"build:{alert['fingerprint']}",
            source="build_alerts",
            metadata={"type": alert["type"], "detail": alert["detail"]},
        )
        if self.audit is not None:
            self.audit.append({
                "event_type": "build_alert",
                "tool": "build_alerts",
                "status": "ok",
                "decision": "allowed",
                "summary": alert["message"],
                "result": {"type": alert["type"], "priority": alert["priority"]},
            })
