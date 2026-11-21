"""Contextual reminders: fire when a repository opens or before the next meeting."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .state import JsonState, iso, parse_moment, utcnow


class ContextualReminders:
    def __init__(
        self,
        config: Any,
        *,
        root: Path | str,
        data_store: Any,
        scheduler: Any,
        notifier: Any,
        audit: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.data_store = data_store
        self.scheduler = scheduler
        self.notifier = notifier
        self.audit = audit
        self._clock = clock or utcnow
        settings = config.get("automation.contextual") or {}
        self.enabled = bool(settings.get("enabled", True))
        self.meeting_lead = max(0, int(settings.get("meeting_lead_minutes", 15)))
        state_dir = Path(root).resolve() / str(config.get("automation.state_dir", "state/automation"))
        self.state = JsonState(state_path or state_dir / "contextual.json")

    def add(
        self,
        message: str,
        *,
        repo: str | None = None,
        meeting: str | None = None,
        priority: str = "normal",
    ) -> dict:
        if not self.enabled:
            raise RuntimeError("Contextual reminders are disabled in configuration")
        text = str(message or "").strip()
        if not text:
            raise ValueError("A reminder message is required")
        if not repo and not meeting:
            raise ValueError("Specify a repository or a meeting trigger")
        reminder = {
            "id": f"ctx_{uuid.uuid4().hex[:12]}",
            "message": text,
            "repo": str(repo) if repo else None,
            "meeting": str(meeting) if meeting else None,
            "priority": str(priority),
            "created_at": iso(self._clock()),
            "fired_count": 0,
            "last_fired": None,
        }
        reminders = self._load()
        reminders.append(reminder)
        self._save(reminders)
        self._record("contextual_reminder_added", f"contextual reminder {reminder['id']} added", {"reminder": reminder})
        return dict(reminder)

    def list(self) -> list[dict]:
        return [dict(item) for item in self._load()]

    def cancel(self, reminder_id: str) -> bool:
        reminders = self._load()
        remaining = [item for item in reminders if item.get("id") != reminder_id]
        if len(remaining) == len(reminders):
            return False
        self._save(remaining)
        self._record("contextual_reminder_cancelled", f"contextual reminder {reminder_id} cancelled", {})
        return True

    def on_repo_open(self, path: str, *, now: datetime | None = None) -> list[dict]:
        """Fire every reminder registered for the repository just opened."""
        moment = now or self._clock()
        if not self.enabled:
            return []
        target = str(Path(str(path)).expanduser().resolve()).lower()
        fired: list[dict] = []
        reminders = self._load()
        for item in reminders:
            stored = str(item.get("repo") or "").strip().lower()
            if not stored:
                continue
            candidate = str(Path(stored).expanduser().resolve()).lower() if stored not in target else stored
            if stored not in target and candidate not in target:
                continue
            result = self.notifier.notify(
                item["message"],
                level=item.get("priority") or "normal",
                channel="terminal",
                priority=item.get("priority") or "normal",
                key=f"contextual:repo:{item['id']}",
                source="contextual_reminder",
                metadata={"reminder_id": item["id"], "repo": str(path)},
            )
            item["fired_count"] = int(item.get("fired_count", 0)) + 1
            item["last_fired"] = iso(moment)
            fired.append({"reminder_id": item["id"], "notification": result.get("status"), "message": item["message"]})
        if fired:
            self._save(reminders)
        if fired:
            self._record("contextual_reminder_fired", f"{len(fired)} contextual reminders fired for a repository", {"fired": fired})
        return fired

    def before_next_meeting(self, *, now: datetime | None = None) -> dict:
        """Notify now if the next meeting is close, otherwise schedule a job."""
        if not self.enabled:
            return {"status": "disabled"}
        moment = now or self._clock()
        events: list[dict] = []
        try:
            events = self.data_store.list("calendar", status="scheduled", limit=100)
        except (ValueError, KeyError) as exc:
            return {"status": "error", "reason": f"could not read calendar: {type(exc).__name__}: {exc}"}
        upcoming: list[tuple[datetime, dict]] = []
        for event in events:
            start = parse_moment(event.get("start"), now=moment)
            if start and start >= moment:
                upcoming.append((start, event))
        if not upcoming:
            return {"status": "no_upcoming_meeting", "reason": "no scheduled calendar event with a future start"}
        start, event = min(upcoming, key=lambda pair: pair[0])
        lead = moment + timedelta(minutes=self.meeting_lead)
        title = str(event.get("title") or "next meeting")
        message = f"Before {title}: review the agenda and outstanding decisions."
        if start <= lead:
            result = self.notifier.notify(
                message, level="normal", channel="terminal", priority="normal",
                key=f"contextual:meeting:{event.get('id')}:{iso(start)}",
                source="contextual_reminder", metadata={"meeting_id": event.get("id"), "start": iso(start)},
            )
            outcome = {"status": "notified_now", "notification": result.get("status"), "meeting": title, "start": iso(start)}
        else:
            job = self.scheduler.schedule(
                f"before meeting: {title}",
                iso(start - timedelta(minutes=self.meeting_lead)),
                {
                    "kind": "notification",
                    "message": message,
                    "priority": "normal",
                    "channel": "terminal",
                    "key": f"contextual:meeting:{event.get('id')}:{iso(start)}",
                    "source": "contextual_reminder",
                },
            )
            outcome = {"status": "scheduled", "job_id": job["id"], "meeting": title, "start": iso(start)}
        self._record("contextual_meeting_reminder", f"meeting reminder prepared for {title}", outcome)
        return outcome

    def status(self) -> dict:
        return {"enabled": self.enabled, "reminders": len(self._load()), "meeting_lead_minutes": self.meeting_lead}

    # ----- internals -----------------------------------------------------
    def _load(self) -> list[dict]:
        stored = self.state.load()
        items = stored.get("reminders") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _save(self, reminders: list[dict]) -> None:
        self.state.save({"reminders": reminders})

    def _record(self, event_type: str, summary: str, result: dict) -> None:
        if self.audit is not None:
            self.audit.append({
                "event_type": event_type,
                "tool": "contextual_reminder",
                "status": "ok",
                "decision": "allowed",
                "summary": summary,
                "result": result,
            })
