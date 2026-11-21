"""Typed tool registry with permission checks, timeouts, retries and audit."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from ..interfaces import Tool
from ..safety.approval import ActionLifecycle, ApprovalError, ApprovalGate, ApprovalToken, Stage
from ..safety.policy import Decision, PolicyEngine
from ..safety.redaction import redact_value


class ToolRegistryError(RuntimeError):
    """Base class for registry errors."""


class ToolNotFoundError(ToolRegistryError):
    pass


class ToolNotAllowedError(ToolRegistryError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission_class: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    backoff_seconds: float = 0.25
    idempotent: bool = True
    side_effects: str = "none"
    rollback: str = "not required"
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolError(Exception):
    code: str
    message: str
    tool: str
    retryable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "tool": self.tool, "retryable": self.retryable}


@dataclass
class ToolResult:
    tool: str
    status: str
    output: Any = None
    error: ToolError | None = None
    duration_ms: float = 0.0
    retries: int = 0
    decision: str | None = None
    reason: str = ""
    dry_run: bool = False
    approval: dict[str, Any] = field(default_factory=dict)
    token_id: str | None = None
    lifecycle: list[str] = field(default_factory=list)
    approval_card: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "output": redact_value(self.output),
            "error": self.error.as_dict() if self.error else None,
            "duration_ms": self.duration_ms,
            "retries": self.retries,
            "decision": self.decision,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "approval": redact_value(self.approval),
            "token_id": self.token_id,
            "lifecycle": self.lifecycle,
        }


class ToolRegistry:
    """All tool calls pass through policy -> approval gate -> audit log."""

    def __init__(
        self,
        config: Any,
        *,
        policy: PolicyEngine,
        approvals: ApprovalGate,
        audit: Any,
        kill_switch: Any | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.kill_switch = kill_switch
        self._clock = clock
        self._sleep = sleeper
        self._tools: dict[str, Tool] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._calls: list[float] = []
        self.allowlist = tuple(config.get("tools.allowlist") or ())
        self.rate_limit = int(config.get("tools.rate_limit_per_minute", 20))
        defaults = config.get("tools.retry") or {}
        self.default_timeout = float(config.get("tools.timeout_seconds", 10))
        self.default_retries = int(defaults.get("max_retries", 2))
        self.default_backoff = float(defaults.get("backoff_seconds", 0.25))

    def register(self, tool: Tool, *, spec: ToolSpec | None = None) -> None:
        if not getattr(tool, "name", None):
            raise ToolRegistryError("Tool must declare a name")
        if not getattr(tool, "permission_class", None):
            raise ToolRegistryError(f"Tool {tool.name!r} must declare a permission class")
        if spec is None:
            spec = ToolSpec(
                name=tool.name,
                description=getattr(tool, "description", ""),
                permission_class=tool.permission_class,
                timeout_seconds=self.default_timeout,
                max_retries=self.default_retries,
                backoff_seconds=self.default_backoff,
                input_schema=tool.schema(),
            )
        self._tools[spec.name] = tool
        self._specs[spec.name] = spec

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def list(self) -> list[ToolSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def schemas(self) -> dict[str, dict]:
        return {
            spec.name: {
                "name": spec.name,
                "description": spec.description,
                "permission_class": spec.permission_class,
                "timeout_seconds": spec.timeout_seconds,
                "max_retries": spec.max_retries,
                "idempotent": spec.idempotent,
                "side_effects": spec.side_effects,
                "rollback": spec.rollback,
                "parameters": spec.input_schema,
            }
            for spec in self.list()
        }

    def _check_rate_limit(self) -> ToolError | None:
        now = self._clock()
        self._calls = [stamp for stamp in self._calls if now - stamp < 60]
        if len(self._calls) >= self.rate_limit:
            return ToolError("rate_limited", "Tool rate limit exceeded", "registry", retryable=False)
        self._calls.append(now)
        return None

    def _run_with_timeout(self, fn: Callable[..., dict], spec: ToolSpec, kwargs: dict) -> dict:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fn, **kwargs)
            try:
                return future.result(timeout=spec.timeout_seconds)
            except FutureTimeout as exc:
                future.cancel()
                raise ToolError(
                    "timeout",
                    f"Tool exceeded {spec.timeout_seconds}s timeout",
                    spec.name,
                    retryable=True,
                ) from exc

    def _audit(self, action: dict, result: ToolResult, *, output: Any, start: float, provider: str | None, model: str | None, cost: float, tokens: dict, resources: dict) -> None:
        # Preserve the verified lifecycle as discrete audit events. When an
        # action needed approval, its plan was already recorded before the
        # approval event, so only the post-approval stages are repeated here.
        stages = list(result.lifecycle)
        if result.approval:
            stages = [stage for stage in stages if stage not in (Stage.PLAN.value, Stage.CONFIRM.value)]
        for stage in stages:
            self.audit.append(
                {
                    "event_type": "tool_lifecycle",
                    "tool": action["tool"],
                    "target": action.get("target"),
                    "args": action.get("params") or {},
                    "decision": result.decision,
                    "status": stage,
                    "result": {"stage": stage, "tool_status": result.status},
                    "summary": f"{action['tool']}: {stage}",
                    "provider": provider,
                    "model": model,
                    "trace_id": action.get("trace_id"),
                }
            )
        self.audit.append(
            {
                "event_type": "tool_call",
                "tool": action["tool"],
                "target": action.get("target"),
                "args": action.get("params") or {},
                "decision": result.decision,
                "status": result.status,
                "duration_ms": round(result.duration_ms, 2),
                "retries": result.retries,
                "error": result.error.as_dict() if result.error else None,
                "approval": result.approval,
                "result": output,
                "summary": f"{action['tool']}: {result.status}",
                "provider": provider,
                "model": model,
                "cost_usd": cost,
                "tokens": tokens,
                "resources": resources,
                "trace_id": action.get("trace_id"),
            }
        )

    def dry_run(self, name: str, *, params: dict | None = None, target: str = "local") -> ToolResult:
        return self.execute(name, params=params, target=target, dry_run=True)

    def execute(
        self,
        name: str,
        *,
        params: dict | None = None,
        target: str = "local",
        approval_token: ApprovalToken | None = None,
        dry_run: bool = False,
        trace_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cost_usd: float = 0.0,
        tokens: dict | None = None,
        resources: dict | None = None,
    ) -> ToolResult:
        start = self._clock()
        params = dict(params or {})
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"Unknown tool {name!r}")
        if self.allowlist and name not in self.allowlist:
            raise ToolNotAllowedError(f"Tool {name!r} is not in the allowlist")
        spec = self._specs[name]
        action = {
            "tool": name,
            "target": target,
            "params": params,
            "permission_class": spec.permission_class,
            "dry_run": bool(dry_run),
            "trace_id": trace_id,
        }
        lifecycle = ActionLifecycle(action)
        outcome = self.policy.evaluate(action)
        result = ToolResult(
            tool=name,
            status="ok",
            decision=outcome.decision.value,
            reason=outcome.reason,
            dry_run=bool(dry_run),
            lifecycle=[Stage.PLAN.value],
        )

        if outcome.decision == Decision.BLOCKED:
            result.status = "blocked"
            self._audit(action, result, output=None, start=start, provider=provider, model=model, cost=cost_usd, tokens=tokens or {}, resources=resources or {})
            return result

        if outcome.decision in (Decision.PENDING_APPROVAL, Decision.ESCALATED):
            result.status = "pending_approval"
            token = approval_token
            if token is None:
                token = self.approvals.request(action)
                result.token_id = token.token_id
                result.approval_card = {
                    "token_id": token.token_id,
                    "tool": name,
                    "target": target,
                    "payload": params,
                    "affected": [target],
                    "risk": spec.permission_class,
                    "expires_at": token.expires_at,
                    "options": ["approve", "deny", "edit", "dry-run"],
                    "plan": "Plan -> Confirm -> Execute -> Verify -> Report",
                }
                self._audit(action, result, output=None, start=start, provider=provider, model=model, cost=cost_usd, tokens=tokens or {}, resources=resources or {})
                return result
            try:
                valid = self.approvals.validate(token, action)
            except ApprovalError as exc:
                valid = False
                result.reason = str(exc)
            if not valid:
                result.status = "denied"
                result.error = ToolError("approval_invalid", "Approval missing, expired, reused, or bound to different parameters", name)
                self._audit(action, result, output=None, start=start, provider=provider, model=model, cost=cost_usd, tokens=tokens or {}, resources=resources or {})
                return result
            result.approval = token.approval_record()
            result.token_id = token.token_id
            result.status = "ok"
            result.decision = Decision.ALLOWED.value
            lifecycle.advance(Stage.CONFIRM)

        if lifecycle.stage == Stage.PLAN:
            # A policy-allowed action is implicitly confirmed; consequential
            # actions above already advanced through the approval gate.
            lifecycle.advance(Stage.CONFIRM)

        limited = self._check_rate_limit()
        if limited is not None:
            result.status = "error"
            result.error = limited
            self._audit(action, result, output=None, start=start, provider=provider, model=model, cost=cost_usd, tokens=tokens or {}, resources=resources or {})
            return result

        fn = tool.dry_run if dry_run else tool.execute
        attempts = 0
        output: Any = None
        while attempts <= (0 if dry_run else spec.max_retries):
            try:
                output = self._run_with_timeout(fn, spec, params)
                if not isinstance(output, dict):
                    raise ToolError("invalid_result", "Tool did not return a structured dict", name)
                result.error = None
                break
            except ToolError as exc:
                result.error = exc
                if not exc.retryable or dry_run:
                    break
            except Exception as exc:  # fail loudly, but with a structured error
                result.error = ToolError("execution_error", f"{type(exc).__name__}: {exc}", name, retryable=True)
                if dry_run:
                    break
            attempts += 1
            if attempts <= spec.max_retries and not dry_run:
                self._sleep(spec.backoff_seconds * (2 ** (attempts - 1)))
        result.retries = attempts

        if result.error is not None:
            result.status = "error"
        else:
            result.output = output
            lifecycle.advance(Stage.EXECUTE)
            lifecycle.advance(Stage.VERIFY)
            lifecycle.advance(Stage.REPORT)
        result.duration_ms = (self._clock() - start) * 1000
        result.lifecycle = lifecycle.stages
        self._audit(action, result, output=output, start=start, provider=provider, model=model, cost=cost_usd, tokens=tokens or {}, resources=resources or {})
        return result
