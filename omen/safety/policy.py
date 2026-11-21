"""Execution-layer policy engine. The model never decides permissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .redaction import redact_text

VALID_CLASSES = ("read_only", "low_risk_write", "external_write", "high_risk")


class Decision(str, Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    PENDING_APPROVAL = "pending_approval"

    def __str__(self) -> str:  # pragma: no cover - convenience for terminal output
        return self.value


@dataclass(frozen=True)
class PolicyOutcome:
    decision: Decision
    reason: str
    risk: str
    alert: str | None = None


class PolicyEngine:
    """Fail-closed policy checks, evaluated before any tool execution."""

    def __init__(self, config: Any, kill_switch: Any | None = None) -> None:
        self.config = config
        self.kill_switch = kill_switch
        self.always_blocked = tuple(config.get("safety.always_blocked") or ())
        self.approval_required = frozenset(
            config.get("tools.approval_required_classes") or ("external_write", "high_risk")
        )

    @staticmethod
    def _classify(action: dict) -> str:
        return str(action.get("permission_class") or action.get("risk") or "unknown")

    def _blocked_kind(self, action: dict) -> str | None:
        kind = str(action.get("kind") or "").lower()
        haystack = " ".join(
            str(part).lower()
            for part in (action.get("tool"), action.get("target"), action.get("kind"), action.get("params"))
            if part is not None
        )
        for blocked in self.always_blocked:
            if kind == str(blocked).lower() or str(blocked).lower() in haystack:
                return str(blocked)
        # Belt and braces: these are never allowed even if config is edited.
        for phrase in (
            "payment execution",
            "payment",
            "wire transfer",
            "bank account",
            "card number",
            "credential sharing",
            "credentials",
            "unapproved financial",
        ):
            if phrase in haystack:
                return phrase
        return None

    def evaluate(self, action: dict) -> PolicyOutcome:
        risk = self._classify(action)
        blocked_kind = self._blocked_kind(action)
        if blocked_kind:
            return PolicyOutcome(
                Decision.BLOCKED,
                f"always blocked: {redact_text(blocked_kind)}",
                risk,
                "blocked_action_attempt",
            )
        if self.kill_switch is not None and self.kill_switch.active:
            return PolicyOutcome(
                Decision.BLOCKED,
                f"kill switch engaged: {self.kill_switch.reason}",
                risk,
                "kill_switch_block",
            )
        if risk not in VALID_CLASSES:
            return PolicyOutcome(Decision.BLOCKED, f"unknown permission class {risk!r}", risk, "invalid_permission_class")
        if not action.get("tool"):
            return PolicyOutcome(Decision.BLOCKED, "missing tool name", risk, "invalid_action")
        if action.get("dry_run"):
            return PolicyOutcome(Decision.ALLOWED, "dry run performs no side effects", risk)
        if risk in self.approval_required:
            return PolicyOutcome(Decision.PENDING_APPROVAL, f"{risk} requires explicit approval", risk)
        if risk == "external_write":
            return PolicyOutcome(Decision.PENDING_APPROVAL, "external write requires explicit approval", risk)
        if risk == "high_risk":
            return PolicyOutcome(Decision.ESCALATED, "high-risk action escalated", risk, "high_risk_escalation")
        return PolicyOutcome(Decision.ALLOWED, f"{risk} action allowed by policy", risk)

    def decide(self, action: dict) -> Decision:
        """Documented entry point: allowed | blocked | escalated | pending_approval."""
        return self.evaluate(action).decision
