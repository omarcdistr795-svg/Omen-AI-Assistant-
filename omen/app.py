"""Wires Phase 1 components into a single local-first assistant."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from .audit.log import AuditLog, detect_alerts
from .automation import AutomationBundle, register_automation_tools
from .config import load_config
from .dashboard import DashboardServer
from .cost.budget import BudgetController
from .prompt import SYSTEM_PROMPT
from .providers.factory import ProviderRouter
from .resources.degradation import DegradationPolicy
from .resources.lifecycle import ModelLifecycleManager
from .resources.monitor import ResourceMonitor
from .safety.approval import ApprovalGate
from .safety.kill_switch import KillSwitch
from .safety.policy import Decision, PolicyEngine
from .safety.redaction import redact_text, redact_value
from .stubs.event_bus import InMemoryEventBus
from .stubs.memory import InMemoryMemoryStore
from .stubs.notifier import NoOpNotifier
from .stubs.scheduler import NoOpScheduler
from .voice import VoiceService
from .tools.coretools.register import register_core_tools
from .tools.demo import DemoCalculate, DemoEcho, DemoNoteCreate
from .tools.registry import ToolRegistry, ToolResult

TOOL_BLOCK = re.compile(r"```tool\s*\n(.*?)\n```", re.DOTALL)


def parse_tool_calls(text: str) -> list[dict]:
    """Extract explicit tool requests from a model reply. No implicit execution."""
    calls: list[dict] = []
    for match in TOOL_BLOCK.finditer(text or ""):
        payload = json.loads(match.group(1))
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not payload["name"].strip():
            raise ValueError("Tool block must be a JSON object with a string 'name' field")
        calls.append(
            {
                "name": str(payload["name"]),
                "target": str(payload.get("target") or "local"),
                "params": dict(payload.get("params") or {}),
            }
        )
    return calls


class OmenApp:
    def __init__(
        self,
        config: Any | None = None,
        *,
        root: Path | str | None = None,
        output: Callable[[str], None] | None = None,
        audit: AuditLog | None = None,
        adapters: dict[str, Any] | None = None,
        memory: Any | None = None,
    ) -> None:
        self.config = config or load_config()
        self.root = Path(root).resolve() if root is not None else (
            self.config.source.parent if self.config.source else Path.cwd()
        )
        self.output = output or (lambda _text: None)
        self.user = str(self.config.get("system.user", "soham.bhagat"))
        self.agent = str(self.config.get("system.agent_id", "omen"))
        self.trace_id = uuid.uuid4().hex
        self.notices: list[str] = []
        self.conversation: list[dict] = []
        self.last_tool_results: list[ToolResult] = []

        state_dir = (self.root / str(self.config.get("system.state_dir", "state"))).resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = state_dir

        self.audit = audit or AuditLog(
            state_dir / "logs" / "audit.jsonl", trace_id=self.trace_id, user=self.user, agent=self.agent
        )
        self.voice = VoiceService(
            self.config,
            root=self.root,
            audit=self.audit,
            output=self._notice,
        )
        self.monitor = ResourceMonitor(self.config)
        self.voice.resource_monitor = self.monitor
        if memory is not None:
            self.memory = memory
        elif self.config.get("memory.enabled", True):
            from .memory import OmenMemoryStore
            db_path = self.root / str(self.config.get("memory.db_path", "state/memory/omen_memory.db"))
            self.memory = OmenMemoryStore(
                self.config,
                db_path=db_path,
                monitor=self.monitor,
                audit=self.audit,
            )
        else:
            self.memory = InMemoryMemoryStore()
        self.scheduler = NoOpScheduler()
        self.events = InMemoryEventBus()
        self.notifier = NoOpNotifier()

        self.kill_switch = KillSwitch(enabled=bool(self.config.get("safety.kill_switch_enabled", True)))
        self.voice.bind(brain=self.chat, kill=self.kill)
        self.approvals = ApprovalGate(self.config)
        self.kill_switch.register(self._on_kill)
        self.policy = PolicyEngine(self.config, kill_switch=self.kill_switch)

        self.budget = BudgetController(self.config, state_path=state_dir / "cost_state.json")
        self.models = ModelLifecycleManager(self.config, on_event=lambda event: self.audit.append(event))
        self.degradation = DegradationPolicy(self.config)
        self.degradation_state: dict = {"level": "normal", "actions": []}

        self.router = ProviderRouter(
            self.config,
            adapters=adapters,
            budget=self.budget,
            audit=self.audit,
            notice=self._notice,
        )
        self.tools = ToolRegistry(
            self.config,
            policy=self.policy,
            approvals=self.approvals,
            audit=self.audit,
            kill_switch=self.kill_switch,
        )
        self.tools.register(DemoEcho())
        self.tools.register(DemoCalculate())
        self.tools.register(DemoNoteCreate())
        # Phase 3 tools are additive: they only register on the Phase 1 registry.
        self.core_tools_manifest = register_core_tools(self)
        # Phase 5 fills the scheduler and notifier stubs. Earlier phases keep
        # their instances when automation is explicitly disabled.
        self.automation: AutomationBundle | None = None
        if self.config.get("automation.enabled", True):
            self.automation = AutomationBundle(self)
            self.scheduler = self.automation.scheduler
            self.notifier = self.automation.notifier
            self.automation_manifest = register_automation_tools(self, self.automation)
        else:
            self.automation_manifest = {"enabled": False, "tools": []}
        self.dashboard: DashboardServer | None = None
        self._dashboard_disabled_by_pressure = False
        self._last_provider: str | None = None
        self._last_model: str | None = None
        self._notice("Omen is online. Local-first. Ask /help for commands.")

    def _notice(self, message: str) -> None:
        self.notices.append(message)
        self.output(f"[omen] {message}")

    def _on_kill(self, reason: str) -> dict:
        cancelled = self.approvals.cancel_pending(reason)
        voice = self.voice.stop()
        self.audit.append(
            {
                "event_type": "kill_switch",
                "tool": "kill_switch",
                "target": "all external actions",
                "decision": "blocked",
                "status": "ok",
                "summary": f"kill switch engaged: {reason}; {cancelled} approvals cancelled",
            }
        )
        return {"approvals_cancelled": cancelled, "voice": voice}

    def _safe_messages(self) -> list[dict]:
        """Redact secrets before context ever reaches a model, enriching with relevant memory."""
        safe = []
        for message in self.conversation:
            item = dict(message)
            if isinstance(item.get("content"), str):
                item["content"] = redact_text(item["content"])
            safe.append(item)

        system_content = SYSTEM_PROMPT
        last_user = next((m["content"] for m in reversed(self.conversation) if m.get("role") == "user"), None)
        if last_user and hasattr(self.memory, "recall"):
            from .memory.retrieval import format_memory_context_for_prompt

            try:
                recalled = self.memory.recall(query=last_user, limit=3)
            except Exception as exc:
                # Recall must never block terminal chat, but failures are never
                # silent: they are audited and reported in plain words.
                self.audit.append(
                    {
                        "event_type": "memory_retrieval_error",
                        "tool": "memory_store",
                        "status": "error",
                        "error": str(exc),
                        "summary": "memory recall failed; chat continued without recalled context",
                    }
                )
                self._notice("Memory recall failed; continuing without recalled context.")
            else:
                if recalled:
                    mem_block = format_memory_context_for_prompt(recalled)
                    if mem_block:
                        system_content = f"{SYSTEM_PROMPT}\n\n{redact_text(mem_block)}"

        return [{"role": "system", "content": system_content}, *safe]

    def stream_chat(
        self, message: str, *, on_token: Callable[[str], None] | None = None, on_progress: Callable[[dict], None] | None = None
    ) -> Iterator[str]:
        """Stream model tokens, then execute any explicitly requested tools safely."""
        self.conversation.append({"role": "user", "content": str(message)})
        collected: list[str] = []
        response = self.router.complete(self._safe_messages(), stream=True)
        for delta in response:
            collected.append(delta)
            if on_token:
                on_token(delta)
            yield delta
        answer = "".join(collected)
        self.conversation.append({"role": "assistant", "content": answer})
        self._last_provider = self.router.adapters[self.router.order[0]].name
        self._last_model = str(self.router.adapters[self._last_provider].model)
        self.audit.append(
            {
                "event_type": "chat_turn",
                "tool": "terminal_chat",
                "status": "ok",
                "provider": self._last_provider,
                "model": self._last_model,
                "tokens": {"characters": len(answer)},
                "summary": "chat turn completed",
            }
        )
        self.last_tool_results = []
        try:
            calls = parse_tool_calls(answer)
        except (ValueError, json.JSONDecodeError) as exc:
            self._notice(f"Malformed tool request was not executed: {exc}")
            self.audit.append(
                {
                    "event_type": "tool_parse_error",
                    "tool": "terminal_chat",
                    "status": "error",
                    "error": str(exc),
                    "summary": "malformed tool request ignored",
                }
            )
            return
        for call in calls:
            result = self.run_tool(
                call["name"], call["params"], target=call["target"], on_progress=on_progress
            )
            self.last_tool_results.append(result)

    def chat(self, message: str, *, on_progress: Callable[[dict], None] | None = None) -> str:
        return "".join(self.stream_chat(message, on_progress=on_progress))

    def run_tool(
        self,
        name: str,
        params: dict | None = None,
        *,
        target: str = "local",
        approval_token: Any | None = None,
        dry_run: bool = False,
        on_progress: Callable[[dict], None] | None = None,
    ) -> ToolResult:
        result = self.tools.execute(
            name,
            params=params,
            target=target,
            approval_token=approval_token,
            dry_run=dry_run,
            trace_id=self.trace_id,
            provider=self._last_provider,
            model=self._last_model,
            cost_usd=0.0,  # provider spend is audited by the router, not per tool
            tokens={},
            resources=self.monitor.status(),
        )
        payload = result.as_dict()
        if result.approval_card:
            payload["approval_card"] = result.approval_card
        if on_progress:
            on_progress(payload)
        self.output(f"[tool] {name}: {result.status} ({result.reason})")
        return result

    def plan_tool(self, name: str, params: dict | None = None, *, target: str = "local") -> ToolResult:
        return self.run_tool(name, params, target=target)

    def approve(self, token_id: str) -> dict:
        token = self.approvals.approve(token_id, self.user)
        self.audit.append(
            {
                "event_type": "approval",
                "tool": "approval_gate",
                "target": str(token.action.get("tool")),
                "decision": "allowed",
                "status": "approved",
                "approval": token.approval_record(),
                "summary": f"action approved by {self.user}",
            }
        )
        return {"token_id": token.token_id, "state": token.state, "approver": token.approver}

    def deny(self, token_id: str) -> dict:
        token = self.approvals.deny(token_id, self.user)
        self.audit.append(
            {
                "event_type": "approval",
                "tool": "approval_gate",
                "target": str(token.action.get("tool")),
                "decision": "blocked",
                "status": "denied",
                "approval": token.approval_record(),
                "summary": f"action denied by {self.user}",
            }
        )
        return {"token_id": token.token_id, "state": token.state}

    def kill(self, reason: str = "user requested halt") -> dict:
        result = self.kill_switch.engage(reason)
        self.output(f"[kill] external actions locked; {result['effects'][0] if result['effects'] else ''}")
        return result

    def status(self) -> dict:
        report: dict[str, Any] = {
            "system": {
                "name": self.config.get("system.name"),
                "user": self.user,
                "agent": self.agent,
                "trace_id": self.trace_id,
            },
            "resources": self.monitor.status(),
            "models": self.models.status(),
            "degradation": self.degradation_state,
            "cost": self.budget.status(),
            "safety": {
                "kill_switch": self.kill_switch.status(),
                "pending_approvals": len(self.approvals.list_pending()),
                "policy": "enabled",
            },
            "stubs": {
                "memory": self.memory.status(),
                "scheduler": self.scheduler.status(),
                "voice": self.voice.status(),
                "event_bus": self.events.status(),
                "notifier": self.notifier.status(),
                "dashboard": self.dashboard.status() if self.dashboard else {"enabled": bool(self.config.get("dashboard.enabled", True)), "running": False},
            },
        }
        # Phase 5 complete status report. Earlier keys above are unchanged so
        # existing renderers and callers keep working.
        recent = self.history(limit=5)
        report.update({
            "active_tasks": self.automation.active_tasks() if self.automation else [],
            "alerts": self.automation.alerts.summary() if self.automation else self.alerts(),
            "alerts_detail": self.alerts(),
            "pending_approvals": self.pending_approvals(),
            "system_health": self.health()["status"],
            "recent_actions": [
                {
                    "timestamp": event.get("timestamp"),
                    "event_type": event.get("event_type"),
                    "tool": event.get("tool"),
                    "status": event.get("status"),
                    "summary": event.get("summary"),
                    "event_id": event.get("event_id"),
                }
                for event in recent
            ],
            "projects": self.automation.projects() if self.automation else {"active": []},
            "memory_state": self.memory.status(),
            "automation": self.automation.status() if self.automation else {"enabled": False},
            "dashboard": self.dashboard.status() if self.dashboard else {"enabled": bool(self.config.get("dashboard.enabled", True)), "running": False},
        })
        return report

    def health(self) -> dict:
        checks = {
            "config": {"ok": True, "detail": str(self.config.source)},
            "providers": {
                "ok": bool(self.router.order),
                "detail": self.router.order,
            },
            "tools": {"ok": bool(self.tools.list()), "detail": [spec.name for spec in self.tools.list()]},
            "safety": {"ok": True, "detail": "policy -> approval -> audit enforced in execution layer"},
            "audit": {"ok": self.audit.verify_integrity(), "detail": str(self.audit.path)},
            "resources": {"ok": True, "detail": self.monitor.status()},
            "memory": {"ok": True, "detail": self.memory.status()},
            "degradation": {"ok": True, "detail": self.degradation_state},
            "budget": {"ok": True, "detail": self.budget.status()},
        }
        overall = "ok" if all(item["ok"] for item in checks.values()) else "degraded"
        return {"status": overall, "checks": checks}

    def degradation_tick(self) -> list[dict]:
        level = self.monitor.level()
        actions = self.degradation.apply(level, loaded_models=self.models.loaded())
        actions.extend(self.voice.apply_degradation(level))
        resource_status = self.monitor.status()
        if level != self.degradation_state.get("level"):
            self.degradation_state = {"level": level, "actions": actions}
            for action in actions:
                if action["component"] == "local_reasoning_model" and "unload now" in action["action"]:
                    self.models.unload("local_reasoning", reason="degradation policy")
                if action["component"] == "memory_reindexing":
                    if hasattr(self.memory, "pause_indexing"):
                        self.memory.pause_indexing()
                if action["component"] == "dashboard":
                    self._degrade_dashboard(level, action)
                self._notice(f"Degradation {level}: {action['component']} — {action['action']}")
            if level in ("normal", "warn") and hasattr(self.memory, "is_indexing_paused"):
                if self.memory.is_indexing_paused():
                    self.memory.resume_indexing()
            self.audit.append(
                {
                    "event_type": "degradation",
                    "tool": "resource_manager",
                    "status": "ok",
                    "decision": "allowed",
                    "summary": f"resource level {level}",
                    "resources": resource_status,
                    "result": actions,
                }
            )
        self._restore_dashboard_if_safe(level)
        if self.automation is not None:
            self.automation.on_resource_level(level, actions)
        return actions

    def _degrade_dashboard(self, level: str, action: dict) -> None:
        if self.dashboard is not None:
            try:
                self.dashboard.stop()
            except Exception as exc:
                self._notice(f"Dashboard stop failed during degradation: {exc}")
            self.dashboard = None
            self._dashboard_disabled_by_pressure = True
            action["action"] = "dashboard stopped to shed memory pressure"
            self.audit.append(
                {
                    "event_type": "dashboard_degraded",
                    "tool": "dashboard",
                    "status": "ok",
                    "decision": "allowed",
                    "summary": f"dashboard stopped at {level} resource pressure",
                }
            )

    def _restore_dashboard_if_safe(self, level: str) -> None:
        if level not in ("normal", "warn"):
            return
        if not self._dashboard_disabled_by_pressure:
            return
        if not self.config.get("dashboard.enabled", True):
            return
        # Restore only when explicitly configured; never auto-load models.
        self._dashboard_disabled_by_pressure = False
        self._notice("Resource pressure recovered; dashboard can be restarted with /dashboard start")

    def close(self) -> None:
        self.voice.stop()
        if self.dashboard is not None:
            try:
                self.dashboard.close()
            except Exception:
                pass
            self.dashboard = None
        if hasattr(self.memory, "close"):
            self.memory.close()
        if self.automation is not None:
            self.automation.close()

    def history(self, **filters: Any) -> list[dict]:
        return self.audit.search(**filters)

    def alerts(self) -> list[dict]:
        return detect_alerts(self.history(), allowlist=tuple(self.config.get("tools.allowlist") or ()))

    def pending_approvals(self) -> list[dict]:
        items = []
        for token in self.approvals.list_pending():
            action = dict(getattr(token, "action", {}) or {})
            items.append(
                {
                    "token_id": token.token_id,
                    "tool": action.get("tool"),
                    "target": action.get("target"),
                    "risk": action.get("permission_class") or action.get("risk"),
                    "expires_at": getattr(token, "expires_at", None),
                    "state": getattr(token, "state", "pending"),
                }
            )
        return items

    def start_dashboard(self) -> dict:
        level = self.monitor.level()
        if level in ("degrade", "emergency"):
            result = {"started": False, "reason": f"dashboard disabled under {level} memory pressure"}
            self.audit.append({"event_type": "dashboard", "tool": "dashboard", "status": "blocked", "summary": result["reason"], "result": result})
            return result
        if self.dashboard is None:
            self.dashboard = DashboardServer(self)
        result = self.dashboard.start()
        self.audit.append({"event_type": "dashboard", "tool": "dashboard", "status": "ok" if result.get("started") else "error", "summary": "dashboard start requested", "result": result})
        return result

    def stop_dashboard(self) -> dict:
        if self.dashboard is None:
            return {"stopped": False, "reason": "not running"}
        result = self.dashboard.stop()
        self.audit.append({"event_type": "dashboard", "tool": "dashboard", "status": "ok", "summary": "dashboard stop requested", "result": result})
        return result

    def brief(self, kind: str = "morning") -> dict:
        """Full briefing. 'morning' and 'evening' are Phase 5; legacy kept as fallback."""
        if self.automation is not None:
            return self.automation.briefing.build(kind)
        recent = self.history(limit=5)
        return {
            "kind": str(kind),
            "recent_events": len(recent),
            "last_status": recent[-1].get("status") if recent else None,
            "alerts": self.alerts(),
            "note": "automation disabled; stub-grade brief from audit history",
        }

    @staticmethod
    def _parse_json(text: str) -> dict:
        try:
            value = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"Expected JSON parameters, got: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("Parameters must be a JSON object")
        return {key: redact_value(item) for key, item in value.items()}
