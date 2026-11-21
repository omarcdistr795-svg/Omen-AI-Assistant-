"""Notifier stub: records notifications in memory, sends nothing externally."""

from __future__ import annotations

from typing import Any


class NoOpNotifier:
    phase = 1
    implementation = "stub-no-op"

    def __init__(self) -> None:
        self.notifications: list[dict] = []

    def notify(self, message: str, *, level: str = "info", channel: str = "terminal") -> dict:
        item = {"message": str(message), "level": level, "channel": channel, "delivered": False}
        self.notifications.append(item)
        return item

    def status(self) -> dict:
        return {
            "phase": self.phase,
            "implementation": self.implementation,
            "notifications": len(self.notifications),
        }
