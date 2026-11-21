"""Record baseline RAM and local latency. No network, no real model required."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from omen.app import OmenApp


def benchmark(*, iterations: int = 10, output: Path | str = "artifacts/benchmark.json") -> dict:
    app = OmenApp(output=lambda _text: None)
    app.trace_id = "benchmark-phase1"
    app.audit.trace_id = "benchmark-phase1"
    ram = app.monitor.status()
    latencies_ms: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        result = app.tools.execute("demo.echo", params={"text": "benchmark"})
        latencies_ms.append((time.perf_counter() - start) * 1000)
        if result.status != "ok":
            raise RuntimeError(f"Benchmark tool call failed: {result.error}")
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": 1,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "hardware_note": "Intel Core Ultra laptop, NPU, 16 GB RAM, no dedicated GPU",
        "ram": {
            "total_gb": ram["ram_total_gb"],
            "used_gb": ram["ram_used_gb"],
            "percent": ram["ram_percent"],
            "level": ram["level"],
        },
        "latency_ms": {
            "iterations": iterations,
            "p50": round(statistics.median(latencies_ms), 3),
            "p95": round(sorted(latencies_ms)[max(0, int(0.95 * len(latencies_ms)) - 1)], 3),
            "note": "local demo tool round-trip; speech targets need Phase 4 to measure",
        },
        "speech_targets": {
            "first_spoken_word_s": 3.0,
            "short_command_transcription_s": 1.5,
            "barge_in_ms": 300,
            "status": "not measurable in Phase 1 (no voice I/O)",
        },
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omen Phase 1 baseline benchmark")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", default="artifacts/benchmark.json")
    args = parser.parse_args(argv)
    report = benchmark(iterations=args.iterations, output=args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
