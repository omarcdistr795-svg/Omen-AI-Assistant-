"""Single-use, expiring, exactly-bound approvals and the action lifecycle."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from .redaction import redact_value


class ApprovalError(RuntimeError):
    """Raised when an approval token is invalid, expired, or reused."""


class Stage(str, Enum):
    PLAN = "plan"
    CONFIRM = "confirm"
    EXECUTE = "execute"
    VERIFY = "verify"
    REPORT = "report"


LIFECYCLE: tuple[Stage, ...] = (
    Stage.PLAN,
    Stage.CONFIRM,
    Stage.EXECUTE,
    Stage.VERIFY,
    Stage.REPORT,
)


@dataclass
class ActionLifecycle:
    """Plan -> Confirm -> Execute -> Verify -> Report, enforced in order."""

    action: dict
    stage: Stage = Stage.PLAN
    stages: list[str] = field(default_factory=lambda: [Stage.PLAN.value])

    def advance(self, stage: Stage) -> None:
        expected = LIFECYCLE[LIFECYCLE.index(self.stage) + 1]
        if stage != expected:
            raise ApprovalError(f"Invalid lifecycle transition {self.stage.value} -> {stage.value}")
        self.stage = stage
        self.stages.append(stage.value)

    def complete(self) -> bool:
        return self.stage == Stage.REPORT


def action_fingerprint(action: dict) -> str:
    """Hash the exact tool, target and parameters. Any change invalidates approval."""
    payload = {
        "tool": action.get("tool"),
        "target": action.get("target"),
        "params": action.get("params") or {},
        "dry_run": bool(action.get("dry_run", False)),
        "kind": action.get("kind"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ApprovalToken:
    token_id: str
    fingerprint: str
    action: dict
    approver: str | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    state: str = "pending"
    used: bool = False
    denied_by: str | None = None

    def approval_record(self) -> dict:
        return {
            "token_id": self.token_id,
            "approver": self.approver,
            "approved_at": datetime.fromtimestamp(self.created_at, timezone.utc).isoformat()
            if self.approver
            else None,
            "exact_action": redact_value(self.action),
            "expires_at": datetime.fromtimestamp(self.expires_at, timezone.utc).isoformat(),
            "state": self.state,
            "used": self.used,
        }


class ApprovalGate:
    """Issues and validates approvals. Conversation text alone is never approval."""

    def __init__(self, config: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.ttl = float(config.get("safety.approval_ttl_seconds", 300))
        self._clock = clock
        self.pending: dict[str, ApprovalToken] = {}

    def request(self, action: dict) -> ApprovalToken:
        now = self._clock()
        token = ApprovalToken(
            token_id=uuid.uuid4().hex,
            fingerprint=action_fingerprint(action),
            action=dict(action),
            created_at=now,
            expires_at=now + self.ttl,
        )
        self.pending[token.token_id] = token
        return token

    def approve(self, token_id: str, approver: str) -> ApprovalToken:
        token = self.pending.get(token_id)
        if token is None:
            raise ApprovalError(f"Unknown approval token {token_id!r}")
        if self._clock() >= token.expires_at:
            token.state = "expired"
            raise ApprovalError("Approval token expired")
        if token.state != "pending":
            raise ApprovalError(f"Approval token is {token.state}")
        token.approver = approver
        token.state = "approved"
        return token

    def deny(self, token_id: str, denier: str) -> ApprovalToken:
        token = self.pending.get(token_id)
        if token is None:
            raise ApprovalError(f"Unknown approval token {token_id!r}")
        token.state = "denied"
        token.denied_by = denier
        return token

    def validate(self, token: ApprovalToken, action: dict, *, consume: bool = True) -> bool:
        """Single-use, expiry and exact tool/target/parameter binding."""
        live = self.pending.get(token.token_id)
        if live is None or live is not token:
            return False
        if self._clock() >= token.expires_at:
            token.state = "expired"
            return False
        if token.state != "approved" or token.used:
            return False
        if token.fingerprint != action_fingerprint(action):
            return False
        if token.approver in (None, ""):
            return False
        if consume:
            token.used = True
            token.state = "used"
        return True

    def cancel_pending(self, reason: str) -> int:
        count = 0
        for token in self.pending.values():
            if token.state == "pending":
                token.state = "cancelled"
                token.denied_by = f"kill switch: {reason}"
                count += 1
        return count

    def list_pending(self) -> list[ApprovalToken]:
        return [token for token in self.pending.values() if token.state == "pending"]
