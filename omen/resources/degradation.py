"""Ordered, logged degradation. Never degrades safety or terminal chat."""

from __future__ import annotations

from typing import Any

_ACTIONS = {
    "dashboard": "stop dashboard server and release HUD resources",
    "heavy_scheduled_jobs": "pause heavy scheduled jobs",
    "memory_reindexing": "pause memory re-indexing",
    "local_reasoning_model": "unload local reasoning model; use cloud or a smaller local model",
    "text_to_speech": "switch to a lighter voice, then text only",
    "wake_word": "push-to-talk only",
    "speech_to_text": "switch to a smaller speech-to-text model",
}


class DegradationPolicy:
    def __init__(self, config: Any) -> None:
        self.order = list(config.get("resources.degradation_order") or [])
        self.never = frozenset(config.get("resources.never_degrade") or ())
        overlap = self.never.intersection(self.order)
        if overlap:
            raise ValueError(f"Protected components in degradation order: {sorted(overlap)}")

    def apply(self, level: str, *, active: list[str] | None = None, loaded_models: list[str] | None = None) -> list[dict]:
        """Return the changes to make, in priority order, with plain-language reasons."""
        if level == "normal":
            return []
        count = 2 if level == "degrade" else len(self.order) if level == "emergency" else 0
        if count == 0:
            return [{"component": "none", "action": "warn only", "reason": f"{level}: no components degraded", "level": level}]
        active_set = set(active) if active is not None else set(self.order)
        loaded = set(loaded_models or [])
        actions: list[dict] = []
        for component in self.order:
            if len(actions) >= count:
                break
            if component in self.never or component not in active_set:
                continue
            action = _ACTIONS.get(component, f"degrade {component}")
            if component == "local_reasoning_model" and "local_reasoning" in loaded:
                action += " (unload now)"
            actions.append(
                {
                    "component": component,
                    "action": action,
                    "reason": f"{level} resource pressure; giving way in documented order",
                    "level": level,
                }
            )
        return actions

    def status(self) -> dict:
        return {"order": self.order, "never_degrade": sorted(self.never)}
