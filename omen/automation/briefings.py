"""Morning briefing and evening review built only from local, real data.

Calendar, tasks, deadlines and blockers come from the Phase 3 local store and
the Phase 1 audit log. Weather is opt-in; with no connector it is explicitly
reported as BLOCKED-EXTERNAL instead of inventing a forecast.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from .state import iso, utcnow


class MockWeatherSource:
    """Default weather source: truthful, offline, and deliberately data-free."""

    implementation = "offline-no-connector"

    def current(self) -> dict:
        return {
            "status": "BLOCKED-EXTERNAL",
            "reason": "no local weather connector configured; no forecast invented",
            "implementation": self.implementation,
        }


class BriefingService:
    def __init__(
        self,
        config: Any,
        *,
        data_store: Any,
        audit: Any | None = None,
        alerts: Callable[[], list[dict]] | None = None,
        clock: Callable[[], datetime] | None = None,
        weather: Any | None = None,
    ) -> None:
        self.config = config
        self.data_store = data_store
        self.audit = audit
        self._alerts = alerts or (lambda: [])
        self._clock = clock or utcnow
        settings = config.get("automation.briefings") or {}
        self.max_items = max(1, int(settings.get("max_items", 5)))
        weather_cfg = settings.get("weather") or {}
        self.weather_enabled = bool(weather_cfg.get("enabled", False))
        self.weather = weather or MockWeatherSource()

    def build(self, kind: str = "morning", *, now: datetime | None = None) -> dict:
        normalized = str(kind).lower()
        if normalized not in ("morning", "evening"):
            raise ValueError(f"Unknown briefing kind {kind!r}; use 'morning' or 'evening'")
        moment = now or self._clock()
        if normalized == "morning":
            sections = self._morning(moment)
        else:
            sections = self._evening(moment)
        recent = self._recent()
        result: dict[str, Any] = {
            "kind": normalized,
            "generated_at": iso(moment),
            "sections": sections,
            # Kept for compatibility with the Phase 1 stub-grade brief().
            "recent_events": len(recent),
            "last_status": recent[-1].get("status") if recent else None,
            "alerts": self._alerts(),
            "note": "local-only briefing; no dashboard and no cloud calls",
        }
        if self.audit is not None:
            self.audit.append({
                "event_type": "briefing",
                "tool": "briefing",
                "status": "ok",
                "decision": "allowed",
                "summary": f"{normalized} briefing generated from local data",
                "result": {"sections": sorted(sections), "recent_events": len(recent)},
            })
        return result

    def morning(self, *, now: datetime | None = None) -> dict:
        return self.build("morning", now=now)

    def evening(self, *, now: datetime | None = None) -> dict:
        return self.build("evening", now=now)

    # ----- data collection ----------------------------------------------
    def _morning(self, now: datetime) -> dict:
        today = now.date().isoformat()
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        calendar = self._list("calendar", start=today, end=tomorrow)
        tasks = self._active_tasks()
        deadlines = self._deadlines(now, days=7)
        return {
            "calendar": {"items": calendar, "count": len(calendar)},
            "tasks": {"items": tasks, "count": len(tasks)},
            "deadlines": {"items": deadlines, "count": len(deadlines)},
            "weather": self._weather(),
            "project_blockers": {"items": self._blockers(deadlines), "count": len(self._blockers(deadlines))},
        }

    def _evening(self, now: datetime) -> dict:
        today = now.date().isoformat()
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        completed = self._completed_today(today)
        decisions = self._unresolved_decisions()
        priorities = self._tomorrow_priorities(tomorrow, now)
        risks = self._open_risks(now)
        return {
            "completed_work": {"items": completed, "count": len(completed)},
            "unresolved_decisions": {"items": decisions, "count": len(decisions)},
            "tomorrow_priorities": {"items": priorities, "count": len(priorities)},
            "open_risks": {"items": risks, "count": len(risks)},
        }

    def _list(self, domain: str, *, start: str | None = None, end: str | None = None, status: str | None = None) -> list[dict]:
        try:
            return self.data_store.list(domain, start=start, end=end, status=status, limit=self.max_items)
        except (ValueError, KeyError) as exc:
            # Fail loudly in the report instead of hiding a storage problem.
            return [{"error": f"could not read {domain}: {type(exc).__name__}: {exc}"}]

    def _active_tasks(self) -> list[dict]:
        items = [*self._list("tasks", status="todo"), *self._list("tasks", status="in_progress")]
        return items[: self.max_items]

    def _deadlines(self, now: datetime, *, days: int) -> list[dict]:
        cutoff = iso(now + timedelta(days=days))
        collected: list[dict] = []
        for domain, pending in (("reminders", "pending"), ("tasks", "todo")):
            for item in self._list(domain, status=pending):
                due = str(item.get("due_at") or item.get("deadline") or "")
                if due and due <= cutoff:
                    collected.append({
                        "id": item.get("id"),
                        "domain": domain,
                        "title": item.get("title") or item.get("text") or "",
                        "due_at": due,
                        "status": item.get("status"),
                    })
        collected.sort(key=lambda item: str(item["due_at"]))
        return collected[: self.max_items]

    def _blockers(self, deadlines: list[dict]) -> list[dict]:
        blockers: list[dict] = []
        for item in self._list("tasks", status="blocked"):
            blockers.append({"id": item.get("id"), "title": item.get("title") or item.get("text") or "", "source": "blocked_task"})
        for item in deadlines:
            if str(item.get("due_at") or "") < iso(self._clock()):
                blockers.append({"id": item.get("id"), "title": item.get("title") or "", "source": "overdue_deadline"})
        for alert in self._alerts():
            blockers.append({"source": "automation_alert", "detail": str(alert.get("type") or alert.get("kind") or "alert"), "tool": alert.get("tool")})
        return blockers[: self.max_items]

    def _weather(self) -> dict:
        if not self.weather_enabled:
            return {"enabled": False, "status": "disabled", "reason": "weather is opt-in and disabled"}
        try:
            report = self.weather.current()
        except Exception as exc:  # surfaced, never silently replaced with fake data
            return {"enabled": True, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        if not isinstance(report, dict):
            return {"enabled": True, "status": "error", "reason": "weather source returned a non-dict result"}
        return {"enabled": True, **report}

    def _completed_today(self, today: str) -> list[dict]:
        completed: list[dict] = []
        if self.audit is not None:
            for event in self.audit.search(since=today, event_type="tool_call"):
                if str(event.get("status")) in ("ok", "approved"):
                    completed.append({
                        "timestamp": event.get("timestamp"),
                        "tool": event.get("tool"),
                        "summary": event.get("summary") or "",
                    })
            completed = completed[-self.max_items:]
        for item in self._list("tasks", status="done"):
            completed.append({"id": item.get("id"), "title": item.get("title") or item.get("text") or "", "source": "task"})
        for item in self._list("tasks", status="completed"):
            completed.append({"id": item.get("id"), "title": item.get("title") or item.get("text") or "", "source": "task"})
        return completed[-self.max_items:]

    def _unresolved_decisions(self) -> list[dict]:
        decisions: list[dict] = []
        for item in self._list("notes"):
            dump = json.dumps(item, ensure_ascii=False, default=str).lower()
            if "decision" in dump and not item.get("resolved"):
                decisions.append({"id": item.get("id"), "title": item.get("title") or item.get("text") or ""})
        return decisions[: self.max_items]

    def _tomorrow_priorities(self, tomorrow: str, now: datetime) -> list[dict]:
        priorities: list[dict] = []
        # ISO timestamps sort lexicographically; use the end of tomorrow so
        # same-day events are not excluded by the Phase 3 string filter.
        for item in self._list("calendar", start=tomorrow, end=f"{tomorrow}T23:59:59+00:00"):
            priorities.append({"id": item.get("id"), "title": item.get("title") or "", "source": "calendar", "start": item.get("start")})
        for item in self._list("tasks", status="todo"):
            due = str(item.get("due_at") or "")
            if due.startswith(tomorrow):
                priorities.append({"id": item.get("id"), "title": item.get("title") or item.get("text") or "", "source": "task", "due_at": due})
        return priorities[: self.max_items]

    def _open_risks(self, now: datetime) -> list[dict]:
        risks: list[dict] = []
        for alert in self._alerts():
            if str(alert.get("priority") or alert.get("level")) in ("high", "critical", "error"):
                risks.append({"source": "automation_alert", "detail": str(alert.get("type") or alert.get("message") or "alert")})
        for item in self._list("tasks", status="blocked"):
            risks.append({"source": "blocked_task", "detail": item.get("title") or item.get("text") or ""})
        for item in self._deadlines(now, days=1):
            if str(item.get("due_at") or "") < iso(now):
                risks.append({"source": "overdue_deadline", "detail": item.get("title") or ""})
        return risks[: self.max_items]

    def _recent(self) -> list[dict]:
        if self.audit is None:
            return []
        return self.audit.search(limit=5)
