"""Phase 5 notifier: quiet hours, priority levels, channels and anti-spam.

The Phase 1 notifier recorded everything and delivered nothing. This fills the
interface with configurable, non-spammy delivery to terminal, audit log and
(optionally) local voice. Voice is best-effort: if Phase 4 voice is unavailable
or under resource pressure, the notification falls back to the terminal and the
fallback is reported and audited rather than hidden.
"""

from __future__ import annotations

import copy
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..safety.redaction import redact_text
from .state import JsonState, fingerprint, iso, utcnow

CallableVoice = Callable[[str], dict]

PRIORITIES = {"low": 0, "normal": 1, "high": 2, "critical": 3}
LEVEL_MAP = {
    "debug": "low",
    "info": "low",
    "notice": "normal",
    "warning": "normal",
    "warn": "normal",
    "error": "high",
    "alert": "high",
    "critical": "critical",
}


class Notifier:
    phase = 5
    implementation = "phase5-quiet-hours-dedup-channels"

    def __init__(
        self,
        config: Any,
        *,
        root: Path | str,
        audit: Any | None = None,
        output: Callable[[str], None] | None = None,
        speak: CallableVoice | None = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.audit = audit
        self._clock = clock or utcnow
        self.output = output or (lambda _text: None)
        self._speak = speak
        self._lock = threading.RLock()
        settings = config.get("automation.notifications") or {}
        quiet = settings.get("quiet_hours") or {}
        anti = settings.get("anti_spam") or {}
        channels = settings.get("channels") or {}
        state_dir = Path(root).resolve() / str(config.get("automation.state_dir", "state/automation"))
        self.state = JsonState(state_path or state_dir / "notifications.json")
        self.enabled = bool(settings.get("enabled", True))
        self.quiet_enabled = bool(quiet.get("enabled", False))
        self.quiet_start = str(quiet.get("start", "22:00"))
        self.quiet_end = str(quiet.get("end", "07:00"))
        self.quiet_defer_critical = bool(quiet.get("defer_critical", False))
        self.channels = {str(key): bool(value) for key, value in channels.items()}
        self.channels.setdefault("terminal", True)
        self.channels.setdefault("log", True)
        self.channels.setdefault("voice", False)
        self.dedupe_window = max(0.0, float(anti.get("dedupe_window_seconds", 300)))
        self.rate_limit = max(1, int(anti.get("rate_limit_per_hour", 20)))
        self.max_repeats = max(1, int(anti.get("max_repeats", 3)))
        self.notifications: list[dict] = []
        self.deferred: list[dict] = []
        self._seen: dict[str, dict] = {}
        self._delivered_at: deque[datetime] = deque()
        self.counts = {"delivered": 0, "duplicate": 0, "deferred": 0, "suppressed": 0, "fallback": 0}
        stored = self.state.load()
        seen = stored.get("seen") or {}
        if isinstance(seen, dict):
            self._seen = {str(key): value for key, value in seen.items() if isinstance(value, dict)}
        for key in ("delivered", "duplicate", "deferred", "suppressed", "fallback"):
            self.counts[key] = int(stored.get(key, self.counts[key]))

    # ----- interface -----------------------------------------------------
    def notify(
        self,
        message: str,
        *,
        level: str = "info",
        channel: str = "terminal",
        priority: str | None = None,
        key: str | None = None,
        source: str = "omen",
        force: bool = False,
        metadata: dict | None = None,
        speak: CallableVoice | None = None,
    ) -> dict:
        now = self._clock()
        normalized = self._priority(priority or LEVEL_MAP.get(str(level).lower(), "normal"))
        channels = self._channels_for(normalized, channel)
        fingerprint_key = key or fingerprint(source, normalized, message)
        item: dict[str, Any] = {
            "message": str(message),
            "level": str(level),
            "channel": channel,
            "channels": channels,
            "priority": normalized,
            "source": str(source),
            "key": fingerprint_key,
            "delivered": False,
            "status": "pending",
            "reason": "",
            "timestamp": iso(now),
            "fallbacks": [],
            "metadata": copy.deepcopy(metadata or {}),
        }
        with self._lock:
            self.notifications.append(item)
            if not self.enabled:
                item.update({"status": "disabled", "reason": "automation notifications disabled"})
                self._persist()
                return item

            prior = self._seen.get(fingerprint_key)
            if prior and self.dedupe_window > 0:
                last = datetime.fromisoformat(str(prior["last"]))
                if timedelta(0) <= now - last <= timedelta(seconds=self.dedupe_window):
                    count = int(prior.get("count", 0)) + 1
                    prior.update({"count": count, "last": iso(now)})
                    if count > self.max_repeats:
                        item.update({"status": "suppressed", "reason": f"anti-spam repeat limit ({self.max_repeats}) reached"})
                        self.counts["suppressed"] += 1
                    else:
                        item.update({"status": "duplicate", "reason": "duplicate within deduplication window"})
                        self.counts["duplicate"] += 1
                    self._persist()
                    self._audit({"event_type": "notification", "tool": "notifier", "status": item["status"], "summary": item["reason"], "result": self._safe(item)})
                    return item
            self._seen[fingerprint_key] = {"count": 1, "last": iso(now)}

            self._delivered_at = deque(stamp for stamp in self._delivered_at if now - stamp < timedelta(hours=1))
            if len(self._delivered_at) >= self.rate_limit:
                item.update({"status": "deferred", "reason": f"rate limit {self.rate_limit}/hour reached"})
                self.counts["deferred"] += 1
                self.deferred.append(item)
                self._persist()
                self._audit({"event_type": "notification", "tool": "notifier", "status": "deferred", "summary": item["reason"], "result": self._safe(item)})
                return item

            if not force and self._quiet_now(now) and not (normalized == "critical" and not self.quiet_defer_critical):
                item.update({"status": "deferred", "reason": "quiet hours; delivered when quiet hours end"})
                self.counts["deferred"] += 1
                self.deferred.append(item)
                self._persist()
                self._audit({"event_type": "notification", "tool": "notifier", "status": "deferred", "summary": item["reason"], "result": self._safe(item)})
                return item

            self._deliver(item, speak or self._speak, now)
            self._persist()
        return item

    def flush_pending(self, *, now: datetime | None = None, speak: CallableVoice | None = None) -> list[dict]:
        """Deliver notifications deferred by quiet hours or rate limits."""
        moment = now or self._clock()
        with self._lock:
            pending, self.deferred = self.deferred, []
            delivered: list[dict] = []
            for item in pending:
                self._deliver(item, speak or self._speak, moment)
                delivered.append(item)
            self._persist()
            return delivered

    def status(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "implementation": self.implementation,
                "notifications": len(self.notifications),
                "delivered": self.counts["delivered"],
                "deferred": len(self.deferred),
                "duplicate": self.counts["duplicate"],
                "suppressed": self.counts["suppressed"],
                "fallbacks": self.counts["fallback"],
                "quiet_hours_active": self._quiet_now(self._clock()),
                "channels": dict(self.channels),
                "enabled": self.enabled,
            }

    # ----- internals -----------------------------------------------------
    @staticmethod
    def _priority(value: str) -> str:
        normalized = str(value).lower()
        if normalized not in PRIORITIES:
            raise ValueError(f"Unknown notification priority {value!r}")
        return normalized

    def _channels_for(self, priority: str, requested: str) -> list[str]:
        if requested in ("auto", "", None):
            if priority == "critical":
                selected = ["terminal", "log", "voice"]
            elif priority == "high":
                selected = ["terminal", "log"]
            else:
                selected = ["terminal"]
        elif "," in str(requested):
            selected = [part.strip() for part in str(requested).split(",") if part.strip()]
        else:
            selected = [str(requested)]
        valid: list[str] = []
        for name in selected:
            if name not in self.channels:
                raise ValueError(f"Unknown notification channel {name!r}")
            if self.channels[name] and name not in valid:
                valid.append(name)
        if not valid:  # terminal is never disabled: fail closed, not silent
            valid = ["terminal"]
        return valid

    def _quiet_now(self, now: datetime) -> bool:
        if not self.quiet_enabled or self.quiet_start == self.quiet_end:
            return False
        minute = now.hour * 60 + now.minute
        start = self._minutes(self.quiet_start)
        end = self._minutes(self.quiet_end)
        if start <= end:
            return start <= minute < end
        return minute >= start or minute < end

    @staticmethod
    def _minutes(value: str) -> int:
        try:
            hour, minute = (int(part) for part in str(value).split(":", 1))
        except ValueError as exc:
            raise ValueError(f"Invalid quiet-hours time {value!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid quiet-hours time {value!r}")
        return hour * 60 + minute

    def _deliver(self, item: dict, speak: CallableVoice | None, now: datetime) -> None:
        fallbacks: list[str] = []
        visible = redact_text(item["message"])
        for name in item["channels"]:
            if name == "terminal":
                self.output(f"[{item['priority']}] {visible}")
            elif name == "log":
                self._audit({"event_type": "notification", "tool": "notifier", "status": "delivered", "decision": "allowed", "summary": item["message"], "result": self._safe(item)})
            elif name == "voice":
                result = self._voice(speak, visible)
                if not result.get("spoken"):
                    reason = str(result.get("reason") or "voice unavailable")
                    fallbacks.append(f"voice -> terminal ({reason})")
                    self.output(f"[{item['priority']}] {visible}")
                    self.counts["fallback"] += 1
                    self._audit({"event_type": "notification_fallback", "tool": "notifier", "status": "error", "error": reason, "summary": "voice delivery unavailable; terminal fallback used"})
        item["fallbacks"] = fallbacks
        item["delivered"] = True
        item["status"] = "delivered"
        item["reason"] = "delivered to " + ", ".join(item["channels"])
        item["delivered_at"] = iso(now)
        self.counts["delivered"] += 1
        self._delivered_at.append(now)

    @staticmethod
    def _voice(speak: CallableVoice | None, text: str) -> dict:
        if speak is None:
            return {"spoken": False, "reason": "voice not present"}
        try:
            result = speak(text)
        except Exception as exc:  # report and fall back; never crash automation
            return {"spoken": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            return {"spoken": False, "reason": "voice returned a non-dict result"}
        return result

    @staticmethod
    def _safe(item: dict) -> dict:
        return {key: copy.deepcopy(item.get(key)) for key in ("message", "priority", "source", "key", "status", "reason", "channels")}

    def _persist(self) -> None:
        self.state.save({
            "seen": self._seen,
            **{key: value for key, value in self.counts.items()},
            "deferred": self.deferred,
        })

    def _audit(self, event: dict) -> None:
        if self.audit is not None:
            self.audit.append(event)
