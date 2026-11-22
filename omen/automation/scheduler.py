"""Phase 5 scheduler: jobs, triggers, retries and resource-aware execution.

The Phase 1 stub never executed anything. This fills that interface and adds
triggers, bounded retries, audit on every attempt, and the documented resource
policy: heavy jobs defer under memory pressure and run at low priority while a
model is loaded. Job handlers that act on the world are executed through the
Phase 1 tool registry (policy -> approval -> audit), never directly.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .state import JsonState, iso, parse_moment, utcnow

Handler = Callable[[dict], dict]


class Scheduler:
    phase = 5
    implementation = "phase5-jobs-triggers-retries"

    def __init__(
        self,
        config: Any,
        *,
        root: Path | str,
        audit: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | str | None = None,
        on_job: Handler | None = None,
    ) -> None:
        self.config = config
        self.audit = audit
        self._clock = clock or utcnow
        settings = config.get("automation.scheduler") or {}
        state_dir = Path(root).resolve() / str(config.get("automation.state_dir", "state/automation"))
        self.state = JsonState(state_path or state_dir / "jobs.json")
        self.max_retries = max(0, int(settings.get("max_retries", 3)))
        self.backoff_seconds = max(0.0, float(settings.get("backoff_seconds", 5)))
        self.defer_seconds = max(1.0, float(settings.get("defer_seconds", 60)))
        self.heavy_defer = bool(settings.get("heavy_jobs_defer_under_pressure", True))
        self.low_priority_loaded = bool(settings.get("low_priority_when_model_loaded", True))
        self._on_job = on_job or (lambda job: {"handled": True, "detail": "no handler registered"})
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._resource_level = "normal"
        self._loaded_models: list[str] = []
        stored = self.state.load()
        jobs = stored.get("jobs") or {}
        if isinstance(jobs, dict):
            self._jobs = {
                str(key): value for key, value in jobs.items() if isinstance(value, dict)
            }
        self.deferred_total = int(stored.get("deferred_total", 0))
        self.failed_total = int(stored.get("failed_total", 0))

    # ----- interface -----------------------------------------------------
    def schedule(
        self,
        name: str,
        when: Any,
        payload: dict | None = None,
        *,
        trigger: dict | None = None,
        resource: dict | None = None,
        max_retries: int | None = None,
        priority: str = "normal",
    ) -> dict:
        if not str(name).strip():
            raise ValueError("A job name is required")
        now = self._clock()
        body = dict(payload or {})
        spec = dict(trigger or {})
        if not spec:
            text = str(when).strip()
            if ":" in text and len(text) <= 5:
                spec = {"type": "daily", "at": text}
            elif isinstance(when, (int, float)) and not isinstance(when, bool):
                spec = {"type": "interval", "seconds": float(when)}
            else:
                spec = {"type": "at", "at": when}
        kind = str(spec.get("type") or "at").lower()
        if kind not in {"at", "interval", "daily", "event"}:
            raise ValueError(f"Unknown trigger type {kind!r}")
        next_run: str | None
        if kind == "event":
            next_run = None
        elif kind == "interval":
            seconds = float(spec.get("seconds", 0))
            if seconds <= 0:
                raise ValueError("Interval triggers need a positive number of seconds")
            next_run = iso(now + timedelta(seconds=seconds))
        else:
            moment = parse_moment(spec.get("at"), now=now)
            if moment is None:
                raise ValueError(f"Cannot parse trigger time {spec.get('at')!r}")
            next_run = iso(moment)
        resource_spec = dict(resource or {})
        import uuid

        job = {
            "id": f"job_{uuid.uuid4().hex[:12]}",
            "name": str(name),
            "trigger": {**spec, "type": kind},
            "payload": copy.deepcopy(body),
            "resource": {
                "heavy": bool(resource_spec.get("heavy", body.get("heavy", False))),
                "model": resource_spec.get("model") or body.get("model"),
            },
            "priority": str(priority),
            "status": "scheduled",
            "attempts": 0,
            "max_retries": self.max_retries if max_retries is None else max(0, int(max_retries)),
            "next_run": next_run,
            "last_error": None,
            "deferred_count": 0,
            "created_at": iso(now),
        }
        with self._lock:
            self._jobs[job["id"]] = job
            self._persist()
        self._audit({"event_type": "scheduler_job", "tool": "scheduler", "target": job["id"], "status": "scheduled", "summary": f"scheduled job {job['name']} ({kind})", "result": self._public(job)})
        return self._public(job)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None or job["status"] in ("cancelled", "failed"):
                return False
            job["status"] = "cancelled"
            job["next_run"] = None
            self._persist()
        self._audit({"event_type": "scheduler_job", "tool": "scheduler", "target": job_id, "status": "cancelled", "summary": f"cancelled job {job['name']}"})
        return True

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [self._public(job) for job in sorted(self._jobs.values(), key=lambda item: item["created_at"])]

    def status(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for job in self._jobs.values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
            return {
                "phase": self.phase,
                "implementation": self.implementation,
                "jobs": len(self._jobs),
                "by_status": counts,
                "deferred_total": self.deferred_total,
                "failed_total": self.failed_total,
                "resource_level": self._resource_level,
                "loaded_models": list(self._loaded_models),
            }

    # ----- resource policy ----------------------------------------------
    def set_resource_level(self, level: str) -> None:
        if level not in {"normal", "warn", "degrade", "emergency"}:
            raise ValueError(f"Unknown resource level: {level}")
        self._resource_level = level

    def set_loaded_models(self, models: list[str] | tuple[str, ...]) -> None:
        self._loaded_models = list(models)

    # ----- execution -----------------------------------------------------
    def fire_event(self, topic: str, *, now: datetime | None = None) -> list[dict]:
        """Make all jobs listening on ``topic`` due, then run them."""
        moment = now or self._clock()
        with self._lock:
            for job in self._jobs.values():
                if job["trigger"].get("type") == "event" and job["trigger"].get("topic") == topic and job["status"] == "scheduled":
                    job["next_run"] = iso(moment)
            self._persist()
        return self.tick(now=moment)

    def tick(self, *, now: datetime | None = None, resource_level: str | None = None, loaded_models: list[str] | tuple[str, ...] | None = None, handler: Handler | None = None) -> list[dict]:
        """Run every due job. Returns one deterministic record per attempt."""
        moment = now or self._clock()
        if resource_level is not None:
            self.set_resource_level(resource_level)
        if loaded_models is not None:
            self.set_loaded_models(list(loaded_models))
        runner = handler or self._on_job
        due: list[dict] = []
        with self._lock:
            for job in self._jobs.values():
                if job["status"] not in ("scheduled", "deferred"):
                    continue
                if job["next_run"] and parse_moment(job["next_run"], now=moment) and parse_moment(job["next_run"], now=moment) <= moment:
                    due.append(copy.deepcopy(job))
        results: list[dict] = []
        for snapshot in due:
            results.append(self._run(snapshot, runner, moment))
        return results

    def _run(self, snapshot: dict, runner: Handler, moment: datetime) -> dict:
        job_id = snapshot["id"]
        heavy = bool(snapshot.get("resource", {}).get("heavy"))
        priority = str(snapshot.get("priority") or "normal")
        defer_reason: str | None = None
        if heavy and self.heavy_defer and self._resource_level in ("degrade", "emergency"):
            defer_reason = f"heavy job deferred: resource level is {self._resource_level}"
        elif heavy and self._resource_level == "warn":
            priority = "low"
        if not defer_reason and self._loaded_models and self.low_priority_loaded:
            priority = "low"
        record: dict[str, Any] = {
            "job_id": job_id,
            "name": snapshot["name"],
            "status": "ok",
            "priority": priority,
            "attempts": snapshot["attempts"],
            "deferred": defer_reason is not None,
            "error": None,
            "result": None,
        }
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] in ("cancelled", "failed", "ok") and job["trigger"].get("type") != "interval":
                record.update({"status": "skipped", "error": "job is no longer runnable"})
                return record
            if defer_reason:
                job["status"] = "deferred"
                job["deferred_count"] += 1
                job["priority"] = priority
                job["next_run"] = iso(moment + timedelta(seconds=self.defer_seconds))
                self.deferred_total += 1
                record.update({"status": "deferred", "error": defer_reason, "attempts": job["attempts"]})
                self._persist()
                self._audit({"event_type": "scheduler_job", "tool": "scheduler", "target": job_id, "status": "deferred", "decision": "allowed", "summary": defer_reason, "result": self._public(job)})
                return record
            job["status"] = "running"
            job["priority"] = priority
            job["attempts"] += 1
            record["attempts"] = job["attempts"]

        error_text: str | None = None
        output: dict | None = None
        try:
            candidate = runner(snapshot)
            if not isinstance(candidate, dict):
                raise TypeError("Job handler must return a dict")
            output = candidate
        except Exception as exc:  # retried below, audited on every attempt
            error_text = f"{type(exc).__name__}: {exc}"
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                record.update({"status": "skipped", "error": "job disappeared"})
                return record
            record["result"] = output
            record["error"] = error_text
            if error_text is None:
                # One-shot jobs complete; recurring and event jobs stay schedulable.
                job["status"] = "ok" if job["trigger"]["type"] == "at" else "scheduled"
                job["last_error"] = None
                self._schedule_next(job, moment)
                record["status"] = "ok"
            elif job["attempts"] <= job["max_retries"]:
                job["status"] = "scheduled"
                job["last_error"] = error_text
                delay = self.backoff_seconds * (2 ** (job["attempts"] - 1))
                job["next_run"] = iso(moment + timedelta(seconds=delay))
                record["status"] = "retry_scheduled"
                record["retry_in_seconds"] = delay
            else:
                job["status"] = "failed"
                job["last_error"] = error_text
                job["next_run"] = None
                self.failed_total += 1
                record["status"] = "failed"
            self._persist()
        self._audit(
            {
                "event_type": "scheduler_job",
                "tool": "scheduler",
                "target": job_id,
                "status": record["status"],
                "decision": "allowed",
                "summary": f"job {snapshot['name']}: {record['status']} (attempt {record['attempts']}, priority {priority})",
                "error": error_text,
                "result": output or {},
                "resources": {"level": self._resource_level, "loaded_models": list(self._loaded_models)},
            }
        )
        return record

    @staticmethod
    def _schedule_next(job: dict, moment: datetime) -> None:
        trigger = job["trigger"]
        kind = trigger.get("type")
        if kind == "interval":
            job["next_run"] = iso(moment + timedelta(seconds=float(trigger["seconds"])))
        elif kind == "daily":
            daily = parse_moment(trigger.get("at"), now=moment)
            if daily is not None:
                if daily <= moment:
                    daily += timedelta(days=1)
                job["next_run"] = iso(daily)
        elif kind == "event":
            job["next_run"] = None
        else:
            job["next_run"] = None

    # ----- internals -----------------------------------------------------
    @staticmethod
    def _public(job: dict) -> dict:
        return copy.deepcopy(job)

    def _persist(self) -> None:
        self.state.save({"jobs": self._jobs, "deferred_total": self.deferred_total, "failed_total": self.failed_total})

    def _audit(self, event: dict) -> None:
        if self.audit is not None:
            self.audit.append(event)
