"""RAM/CPU monitoring with 80/85/92% thresholds."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ResourceSnapshot:
    ram_total_gb: float
    ram_available_gb: float
    ram_used_gb: float
    ram_percent: float
    cpu_percent: float
    load1: float

    def as_dict(self) -> dict:
        return asdict(self)


def system_sample() -> dict:
    """Read Linux /proc directly; no external dependency and no side effects."""
    info: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0][:-1]] = int(parts[1])  # kB
    total_kb = info.get("MemTotal", 0)
    available_kb = info.get("MemAvailable", 0)
    if not total_kb:
        raise RuntimeError("Cannot read MemTotal from /proc/meminfo")
    load1 = float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    cpu_count = os.cpu_count() or 1
    return {
        "ram_total_gb": total_kb / 1024 / 1024,
        "ram_available_gb": available_kb / 1024 / 1024,
        "load1": load1,
        "cpu_percent": min(100.0, 100.0 * load1 / cpu_count),
    }


class ResourceMonitor:
    def __init__(self, config: Any, *, sampler: Callable[[], dict] | None = None) -> None:
        self.config = config
        self._sampler = sampler or system_sample
        self.warn = float(config.get("resources.warn_percent", 80))
        self.degrade = float(config.get("resources.degrade_percent", 85))
        self.emergency = float(config.get("resources.emergency_percent", 92))

    def snapshot(self) -> ResourceSnapshot:
        sample = self._sampler()
        total = float(sample["ram_total_gb"])
        available = float(sample["ram_available_gb"])
        used = max(0.0, total - available)
        ram_percent = 100.0 * used / total if total else 0.0
        return ResourceSnapshot(
            ram_total_gb=round(total, 3),
            ram_available_gb=round(available, 3),
            ram_used_gb=round(used, 3),
            ram_percent=round(ram_percent, 2),
            cpu_percent=round(float(sample.get("cpu_percent", 0.0)), 2),
            load1=round(float(sample.get("load1", 0.0)), 3),
        )

    def level(self, snapshot: ResourceSnapshot | None = None) -> str:
        snap = snapshot or self.snapshot()
        pressure = max(snap.ram_percent, snap.cpu_percent)
        if pressure >= self.emergency:
            return "emergency"
        if pressure >= self.degrade:
            return "degrade"
        if pressure >= self.warn:
            return "warn"
        return "normal"

    def status(self) -> dict:
        snap = self.snapshot()
        return {
            **snap.as_dict(),
            "level": self.level(snap),
            "thresholds": {"warn": self.warn, "degrade": self.degrade, "emergency": self.emergency},
            "target_gb": self.config.get("resources.ram_target_gb"),
            "hard_ceiling_gb": self.config.get("resources.ram_hard_ceiling_gb"),
        }
