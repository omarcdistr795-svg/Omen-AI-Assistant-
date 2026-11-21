"""Wires Phase 5 services into the existing app and tool registry.

Only new files are added elsewhere, plus small plug-in points in app.py,
cli/commands.py and cli/render.py. Policy -> approval -> audit remain owned by
Phase 1; every Phase 5 tool declares a permission class and is registered there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..tools.coretools.base import CoreTool
from ..tools.registry import ToolRegistry, ToolSpec
from .data import AutomationDataStore
from .alerts import BuildAlerts
from .briefings import BriefingService
from .contextual import ContextualReminders
from .guardian import DeadlineGuardian
from .notifier import Notifier
from .scheduler import Scheduler

AUTOMATION_TOOL_NAMES: tuple[str, ...] = (
    "brief.morning",
    "brief.evening",
    "status.report",
    "guardian.check",
    "build_alerts.scan",
    "automation.schedule",
    "automation.cancel",
    "notify.send",
    "contextual.remind",
)


class AutomationBundle:
    def __init__(self, app: Any) -> None:
        self.app = app
        config = app.config
        root = Path(app.root).resolve()
        audit = app.audit
        self.local_data = AutomationDataStore(config, root)  # reads the additive Phase 3 store
        self.notifier = Notifier(
            config,
            root=root,
            audit=audit,
            output=app.output,
            speak=self._speak,
        )
        self.scheduler = Scheduler(
            config,
            root=root,
            audit=audit,
            on_job=self._execute_job,
        )
        self.alerts = BuildAlerts(config, root=root, notifier=self.notifier, audit=audit)
        self.briefing = BriefingService(
            config,
            data_store=self.local_data,
            audit=audit,
            alerts=self.alerts.summary,
        )
        self.guardian = DeadlineGuardian(
            config,
            root=root,
            data_store=self.local_data,
            notifier=self.notifier,
            audit=audit,
        )
        self.contextual = ContextualReminders(
            config,
            root=root,
            data_store=self.local_data,
            scheduler=self.scheduler,
            notifier=self.notifier,
            audit=audit,
        )
        if hasattr(app, "monitor") and hasattr(app, "models"):
            self.scheduler.set_loaded_models(app.models.loaded())
            self.scheduler.set_resource_level(app.monitor.level())

    # ----- voice fallback -------------------------------------------------
    def _speak(self, text: str) -> dict:
        voice = getattr(self.app, "voice", None)
        if voice is None or not hasattr(voice, "speak"):
            return {"spoken": False, "reason": "voice not present; terminal fallback"}
        try:
            result = voice.speak(text)
        except Exception as exc:  # reported by the notifier; never swallowed here
            return {"spoken": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            return {"spoken": False, "reason": "voice returned a non-dict result"}
        return result

    # ----- scheduler handler ---------------------------------------------
    def _execute_job(self, job: dict) -> dict:
        payload = dict(job.get("payload") or {})
        kind = str(payload.get("kind") or "notification")
        if kind == "tool":
            result = self.app.run_tool(
                str(payload.get("tool") or ""),
                dict(payload.get("params") or {}),
                target=str(payload.get("target") or "local"),
            )
            return result.as_dict()
        if kind == "briefing":
            return self.briefing.build(str(payload.get("briefing") or "morning"))
        if kind == "guardian":
            return self.guardian.check()
        if kind == "build_scan":
            return self.alerts.scan()
        if kind == "notification":
            return self.notifier.notify(
                str(payload.get("message") or ""),
                level=str(payload.get("level") or payload.get("priority") or "info"),
                channel=str(payload.get("channel") or "terminal"),
                priority=payload.get("priority"),
                key=payload.get("key"),
                source=str(payload.get("source") or "scheduler"),
            )
        raise ValueError(f"Unknown scheduled job kind {kind!r}")

    # ----- resource policy -------------------------------------------------
    def on_resource_level(self, level: str, actions: list[dict]) -> None:
        self.scheduler.set_resource_level(level)
        self.scheduler.set_loaded_models(self.app.models.loaded())
        self.notifier.notify(
            f"Resource level is {level}; scheduled jobs were re-prioritised.",
            level="info", priority="low", channel="log",
            key=f"resource:{level}", source="resource_manager",
            metadata={"actions": actions},
        )

    # ----- status helpers -------------------------------------------------
    def active_tasks(self) -> list[dict]:
        return [
            *self.local_data.list("tasks", status="todo", limit=10),
            *self.local_data.list("tasks", status="in_progress", limit=10),
        ]

    def projects(self) -> dict:
        names = sorted({
            str(task.get("project") or "")
            for task in [*self.active_tasks(), *self.local_data.list("tasks", limit=100)]
            if task.get("project")
        })
        return {"active": names, "note": "derived from local tasks; no remote project service configured"}

    def status(self) -> dict:
        return {
            "enabled": True,
            "scheduler": self.scheduler.status(),
            "notifier": self.notifier.status(),
            "build_alerts": self.alerts.status(),
            "guardian": {"enabled": self.guardian.enabled, "lead_times_minutes": self.guardian.leads},
            "contextual": self.contextual.status(),
            "briefings": {"weather_enabled": self.briefing.weather_enabled, "max_items": self.briefing.max_items},
        }

    def close(self) -> None:
        # All state files are written atomically on change; nothing else needed.
        return None


class _AutomationTool(CoreTool):
    service_name = "automation"

    def _bundle(self, **kwargs: Any) -> AutomationBundle:
        bundle = kwargs.get("_bundle") or kwargs.get("bundle")
        if not isinstance(bundle, AutomationBundle):
            raise RuntimeError("Automation service is not bound to this tool")
        return bundle


class MorningBriefTool(_AutomationTool):
    name = "brief.morning"
    description = "Build the local morning briefing (calendar, tasks, deadlines, weather, blockers)."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).briefing.morning()


class EveningBriefTool(_AutomationTool):
    name = "brief.evening"
    description = "Build the local evening review (completed work, decisions, priorities, risks)."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).briefing.evening()


class StatusReportTool(_AutomationTool):
    name = "status.report"
    description = "Report active tasks, alerts, approvals, health, recent actions, projects and memory."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).app.status()


class GuardianCheckTool(_AutomationTool):
    name = "guardian.check"
    description = "Check deadlines for escalation, conflicts and preparation checklists."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).guardian.check()


class BuildAlertsScanTool(_AutomationTool):
    name = "build_alerts.scan"
    description = "Scan the audit trail for test regressions and long-running jobs."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).alerts.scan()


class AutomationScheduleTool(_AutomationTool):
    name = "automation.schedule"
    description = "Schedule a local job with a trigger, retries and resource policy."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "when": {},
            "payload": {"type": "object"},
            "resource": {"type": "object"},
        },
        "required": ["name", "when"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).scheduler.schedule(
            str(kwargs.get("name") or ""),
            kwargs.get("when"),
            dict(kwargs.get("payload") or {}),
            resource=dict(kwargs.get("resource") or {}),
        )


class AutomationCancelTool(_AutomationTool):
    name = "automation.cancel"
    description = "Cancel a scheduled local job."
    permission_class = "low_risk_write"
    _schema = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict:
        return {"cancelled": self._bundle(**kwargs).scheduler.cancel(str(kwargs.get("job_id") or ""))}


class NotifySendTool(_AutomationTool):
    name = "notify.send"
    description = "Send one prioritised notification through the configured channels."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "priority": {"type": "string"},
            "channel": {"type": "string"},
            "key": {"type": "string"},
        },
        "required": ["message"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).notifier.notify(
            str(kwargs.get("message") or ""),
            priority=kwargs.get("priority") or "normal",
            channel=str(kwargs.get("channel") or "terminal"),
            key=kwargs.get("key"),
            source="tool",
        )


class ContextualRemindTool(_AutomationTool):
    name = "contextual.remind"
    description = "Create a reminder triggered by a repository or a meeting."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "repo": {"type": "string"},
            "meeting": {"type": "string"},
            "priority": {"type": "string"},
        },
        "required": ["message"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict:
        return self._bundle(**kwargs).contextual.add(
            str(kwargs.get("message") or ""),
            repo=kwargs.get("repo"),
            meeting=kwargs.get("meeting"),
            priority=str(kwargs.get("priority") or "normal"),
        )


def register_automation_tools(app: Any, bundle: AutomationBundle) -> dict:
    """Register every Phase 5 tool on the Phase 1 registry with a spec."""
    registry: ToolRegistry = app.tools
    read_specs = {"max_retries": 1}
    write_specs = {"max_retries": 0}
    tools: list[tuple[CoreTool, dict]] = [
        (MorningBriefTool(), read_specs),
        (EveningBriefTool(), read_specs),
        (StatusReportTool(), read_specs),
        (GuardianCheckTool(), read_specs),
        (BuildAlertsScanTool(), read_specs),
        (AutomationScheduleTool(), write_specs),
        (AutomationCancelTool(), write_specs),
        (NotifySendTool(), write_specs),
        (ContextualRemindTool(), write_specs),
    ]
    registered: list[str] = []
    for tool, retry in tools:
        # Bind the bundle into every call without changing the Tool protocol.
        original = tool.execute

        def bound(*args: Any, _original=original, _tool=tool, _bundle=bundle, **kwargs: Any) -> dict:
            return _original(_bundle=_bundle, **kwargs)

        tool.execute = bound  # type: ignore[method-assign]
        registry.register(
            tool,
            spec=ToolSpec(
                name=tool.name,
                description=tool.description,
                permission_class=tool.permission_class,
                timeout_seconds=10.0,
                max_retries=int(retry["max_retries"]),
                backoff_seconds=0.25,
                idempotent=tool.permission_class == "read_only",
                side_effects="none" if tool.permission_class == "read_only" else "local automation state changed",
                rollback="not required" if tool.permission_class == "read_only" else "cancel the job/reminder or delete the state entry",
                input_schema=tool.schema(),
            ),
        )
        registered.append(tool.name)
    configured = tuple(app.config.get("automation.tools.allowlist") or ())
    registry.allowlist = tuple(dict.fromkeys((*registry.allowlist, *AUTOMATION_TOOL_NAMES, *configured)))
    return {"enabled": True, "tools": registered, "state_dir": str(app.config.get("automation.state_dir", "state/automation"))}
