"""Global kill switch: halt speech, cancel pending calls, lock external actions."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable


class KillSwitch:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.active = False
        self.reason = ""
        self.engaged_at: float | None = None
        self._callbacks: list[Callable[[str], None]] = []

    def register(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def engage(self, reason: str = "user requested halt") -> dict:
        if not self.enabled:
            raise RuntimeError("Kill switch is disabled in configuration")
        self.active = True
        self.reason = reason
        self.engaged_at = time.time()
        results = []
        for callback in self._callbacks:
            # Callbacks are Omen-owned in-phase hooks; errors must be visible.
            results.append(callback(reason))
        return {
            "active": True,
            "reason": reason,
            "engaged_at": datetime.fromtimestamp(self.engaged_at, timezone.utc).isoformat(),
            "effects": results,
        }

    def release(self, *, approver: str) -> dict:
        if not approver:
            raise ValueError("Releasing the kill switch requires an approver identity")
        self.active = False
        self.reason = ""
        self.engaged_at = None
        return {"active": False, "released_by": approver}

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "reason": self.reason,
            "external_actions_locked": self.active,
        }
