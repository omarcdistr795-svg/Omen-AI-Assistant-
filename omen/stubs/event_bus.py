"""EventBus stub: in-memory synchronous publish/subscribe, no persistence."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


class InMemoryEventBus:
    phase = 1
    implementation = "stub-in-memory"

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[dict], None]]] = defaultdict(list)
        self.events: list[dict] = []

    def subscribe(self, topic: str, handler: Callable[[dict], None]) -> None:
        self._subscribers[topic].append(handler)

    def publish(self, topic: str, event: dict) -> int:
        payload = {"topic": topic, **event}
        self.events.append(payload)
        for handler in list(self._subscribers.get(topic, [])):
            handler(payload)
        return len(self._subscribers.get(topic, []))

    def status(self) -> dict:
        return {
            "phase": self.phase,
            "implementation": self.implementation,
            "topics": sorted(self._subscribers),
            "events": len(self.events),
        }
