"""Deadline guardian: escalating reminders, conflicts and preparation checklists."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .state import JsonState, fingerprint, iso, parse_moment, utcnow

DEFAULT_LEADS = (1440, 120, 30)
GENERIC_CHECKLIST = (
    "Confirm the exact deliverable and deadline",
    "Gather the inputs you still need",
    "Resolve any open decision that blocks the work",
    "Verify the result before the deadline",
)


class DeadlineGuardian:
    def __init__(
        self,
        config: Any,
        *,
        root: Path | str,
        data_store: Any,
        notifier: Any,
        audit: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.data_store = data_store
        self.notifier = notifier
        self.audit = audit
        self._clock = clock or utcnow
        settings = config.get("automation.guardian") or {}
        self.enabled = bool(settings.get("enabled", True))
        self.leads = sorted((int(value) for value in settings.get("lead_times_minutes") or DEFAULT_LEADS), reverse=True)
        self.conflict_window = timedelta(minutes=max(0, int(settings.get("conflict_window_minutes", 30))))
        self.checklist_enabled = bool(settings.get("checklist_enabled", True))
        state_dir = Path(root).resolve() / str(config.get("automation.state_dir", "state/automation"))
        self.state = JsonState(state_path or state_dir / "guardian.json")

    def check(self, *, now: datetime | None = None) -> dict:
        moment = now or self._clock()
        if not self.enabled:
            return {"status": "disabled", "reminders": [], "conflicts": [], "checklists": []}
        stored = self.state.load()
        sent = stored.get("sent") or {}
        if not isinstance(sent, dict):
            sent = {}
        deadlines = self._deadlines(moment)
        reminders: list[dict] = []
        checklists: list[dict] = []
        for item in deadlines:
            due = parse_moment(item.get("due_at"), now=moment)
            if due is None:
                continue
            key = fingerprint(item.get("domain"), item.get("id"), item.get("due_at"))
            levels = sent.get(key, {}).get("levels", [])
            if not isinstance(levels, list):
                levels = []
            minutes_left = (due - moment).total_seconds() / 60
            reached: list[tuple[str, str, str]] = []
            if minutes_left <= 0:
                reached.append(("overdue", "critical", f"Deadline passed {self._human(-minutes_left)} ago"))
            else:
                # Send only the tightest threshold crossed, so one check never
                # fires the entire escalation ladder at once.
                applicable = [lead for lead in self.leads if minutes_left <= lead]
                if applicable:
                    lead = min(applicable)
                    reached.append((f"lead_{lead}m", self._priority_for(lead), f"Deadline in {self._human(minutes_left)}"))
            checklist = self._checklist(item)
            if checklist:
                checklists.append({"deadline_id": item.get("id"), "title": item.get("title") or "", "items": checklist})
            for level, priority, text in reached:
                if level in levels:
                    continue
                message = f"{item.get('title') or 'Deadline'}: {text}."
                result = self.notifier.notify(
                    message,
                    level=priority,
                    channel="auto" if priority in ("high", "critical") else "terminal",
                    priority=priority,
                    key=f"deadline:{key}:{level}",
                    source="deadline_guardian",
                    metadata={"deadline_id": item.get("id"), "due_at": item.get("due_at"), "level": level, "checklist": checklist},
                )
                reminders.append({"deadline_id": item.get("id"), "level": level, "priority": priority, "notification": result.get("status"), "message": message})
                levels.append(level)
            if levels:
                sent[key] = {"levels": levels, "last_notified": iso(moment), "title": item.get("title") or ""}
        conflicts = self._conflicts(deadlines, moment)
        for conflict in conflicts:
            self.notifier.notify(
                f"Scheduling conflict: {conflict['detail']}",
                level="warning",
                channel="auto",
                priority="high",
                key=f"conflict:{conflict['fingerprint']}",
                source="deadline_guardian",
                metadata={"conflict": conflict},
            )
        self.state.save({"sent": sent, "last_check": iso(moment)})
        report = {
            "status": "ok",
            "checked_at": iso(moment),
            "deadlines": len(deadlines),
            "reminders": reminders,
            "conflicts": conflicts,
            "checklists": checklists,
        }
        if self.audit is not None:
            self.audit.append({
                "event_type": "deadline_guardian",
                "tool": "deadline_guardian",
                "status": "ok",
                "decision": "allowed",
                "summary": f"deadline check: {len(reminders)} reminders, {len(conflicts)} conflicts",
                "result": {"reminders": len(reminders), "conflicts": len(conflicts)},
            })
        return report

    # ----- internals -----------------------------------------------------
    @staticmethod
    def _priority_for(lead_minutes: int) -> str:
        if lead_minutes >= 720:
            return "low"
        if lead_minutes >= 60:
            return "normal"
        return "high"

    @staticmethod
    def _human(minutes: float) -> str:
        minutes = max(0.0, minutes)
        if minutes < 60:
            return f"{int(minutes)} min"
        if minutes < 1440:
            return f"{minutes / 60:.1f} hours"
        return f"{minutes / 1440:.1f} days"

    def _deadlines(self, now: datetime) -> list[dict]:
        items: list[dict] = []
        for domain, status in (("reminders", "pending"), ("tasks", "todo"), ("calendar", "scheduled")):
            for record in self.data_store.list(domain, status=status, limit=100):
                due = record.get("due_at") or record.get("deadline") or record.get("start") or record.get("end")
                if due:
                    items.append({
                        "id": record.get("id"),
                        "domain": domain,
                        "title": record.get("title") or record.get("text") or "",
                        "due_at": due,
                        "start": record.get("start"),
                        "end": record.get("end"),
                        "checklist": record.get("checklist"),
                    })
        return items

    def _checklist(self, item: dict) -> list[str]:
        if not self.checklist_enabled:
            return []
        supplied = item.get("checklist")
        if isinstance(supplied, list) and supplied:
            return [str(entry) for entry in supplied]
        return list(GENERIC_CHECKLIST)

    def _conflicts(self, deadlines: list[dict], now: datetime) -> list[dict]:
        windows: list[dict] = []
        for item in deadlines:
            start = parse_moment(item.get("start"), now=now)
            end = parse_moment(item.get("end"), now=now)
            if start and end and end > start:
                windows.append({"id": item.get("id"), "title": item.get("title") or "", "start": start, "end": end})
        windows.sort(key=lambda window: window["start"])
        conflicts: list[dict] = []
        for index, first in enumerate(windows):
            for second in windows[index + 1:]:
                if second["start"] > first["end"]:
                    break
                overlap = min(first["end"], second["end"]) - max(first["start"], second["start"])
                if overlap >= timedelta(0):
                    key = fingerprint("overlap", first["id"], second["id"])
                    conflicts.append({
                        "fingerprint": key,
                        "type": "overlapping_meetings",
                        "detail": f"{first['title'] or first['id']} overlaps {second['title'] or second['id']}",
                    })
        for item in deadlines:
            due = parse_moment(item.get("due_at"), now=now)
            if due is None:
                continue
            for window in windows:
                if window["start"] - self.conflict_window <= due <= window["end"] + self.conflict_window:
                    key = fingerprint("deadline_meeting", item.get("id"), window["id"])
                    conflicts.append({
                        "fingerprint": key,
                        "type": "deadline_during_meeting",
                        "detail": f"{item.get('title') or item.get('id')} falls during {window['title'] or window['id']}",
                    })
        unique: dict[str, dict] = {}
        for conflict in conflicts:
            unique.setdefault(conflict["fingerprint"], conflict)
        return list(unique.values())
