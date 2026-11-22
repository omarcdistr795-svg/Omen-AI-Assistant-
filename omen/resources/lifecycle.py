"""Lazy model load, idle unload, one heavy model at a time, memory ceiling."""

from __future__ import annotations

import time
from typing import Any, Callable


class ModelBudgetError(RuntimeError):
    """Raised when loading a model would breach the configured ceiling."""


class ModelLifecycleManager:
    def __init__(self, config: Any, *, clock: Callable[[], float] = time.time, on_event: Callable[[dict], None] | None = None) -> None:
        self.config = config
        self.models = dict(config.get("resources.models") or {})
        self.max_heavy = int(config.get("resources.heavy_models_max", 1))
        self.idle_seconds = float(config.get("resources.idle_unload_seconds", 300))
        self.target_gb = float(config.get("resources.ram_target_gb", 8))
        self.ceiling_gb = float(config.get("resources.ram_hard_ceiling_gb", 9))
        self._clock = clock
        self._on_event = on_event or (lambda _event: None)
        self._loaded: dict[str, float] = {}

    def _memory(self, names: list[str] | None = None) -> float:
        selected = names if names is not None else list(self._loaded)
        return sum(float(self.models[name].get("memory_gb", 0)) for name in selected if name in self.models)

    def _heavy_loaded(self) -> list[str]:
        return [name for name in self._loaded if self.models.get(name, {}).get("heavy")]

    def _emit(self, action: str, name: str, reason: str) -> None:
        self._on_event(
            {
                "event_type": "model_lifecycle",
                "action": action,
                "tool": "model_lifecycle",
                "target": name,
                "status": "ok",
                "decision": "allowed",
                "summary": f"model {name} {action}: {reason}",
                "resources": {"loaded_models": list(self._loaded), "estimated_gb": round(self._memory(), 3)},
            }
        )

    def load(self, name: str, *, now: float | None = None) -> dict:
        """Lazy load. Unloads an idle heavy model first; preserves memory ceiling."""
        now = self._clock() if now is None else now
        if name not in self.models:
            raise ModelBudgetError(f"Unknown model {name!r}")
        if name in self._loaded:
            self._loaded[name] = now
            return {"loaded": name, "already_loaded": True, "memory_gb": self._memory()}

        unloaded: list[str] = []
        heavy = bool(self.models[name].get("heavy"))
        if heavy:
            while len(self._heavy_loaded()) >= self.max_heavy:
                victim = sorted(self._loaded, key=lambda key: self._loaded[key])[0]
                self.unload(victim, reason="one heavy model at a time")
                unloaded.append(victim)
        required = float(self.models[name].get("memory_gb", 0))
        while self._memory() + required > self.ceiling_gb and self._loaded:
            victim = sorted(self._loaded, key=lambda key: self._loaded[key])[0]
            self.unload(victim, reason="memory ceiling")
            unloaded.append(victim)
        if self._memory() + required > self.ceiling_gb:
            raise ModelBudgetError(f"Model {name!r} would exceed the {self.ceiling_gb} GB hard ceiling")
        self._loaded[name] = now
        self._emit("load", name, "lazy load on demand")
        return {"loaded": name, "already_loaded": False, "unloaded": unloaded, "memory_gb": self._memory()}

    def touch(self, name: str, *, now: float | None = None) -> None:
        if name in self._loaded:
            self._loaded[name] = self._clock() if now is None else now

    def unload(self, name: str, *, reason: str = "explicit unload") -> bool:
        if name not in self._loaded:
            return False
        del self._loaded[name]
        self._emit("unload", name, reason)
        return True

    def unload_idle(self, *, now: float | None = None, ttl: float | None = None) -> list[str]:
        now = self._clock() if now is None else now
        ttl = self.idle_seconds if ttl is None else ttl
        idle = [name for name, loaded_at in self._loaded.items() if now - loaded_at >= ttl]
        for name in idle:
            self.unload(name, reason=f"idle for {ttl}s")
        return idle

    def loaded(self) -> list[str]:
        return sorted(self._loaded)

    def status(self) -> dict:
        return {
            "loaded_models": self.loaded(),
            "heavy_loaded": self._heavy_loaded(),
            "estimated_gb": round(self._memory(), 3),
            "target_gb": self.target_gb,
            "hard_ceiling_gb": self.ceiling_gb,
            "max_heavy": self.max_heavy,
            "idle_unload_seconds": self.idle_seconds,
        }
