"""Monthly budget, per-request token cap, 80% warning and 100% local fallback."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    warning: str | None = None
    fallback: str | None = None


class BudgetController:
    """Cloud spend gate. Local inference is never blocked by cloud budget."""

    def __init__(
        self,
        config: Any,
        *,
        state_path: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        persist: bool = True,
    ) -> None:
        self.config = config
        self.enabled = bool(config.get("cost.enabled"))
        self.budget = float(config.get("cost.monthly_budget_usd", 0.0))
        self.token_cap = int(config.get("cost.per_request_token_cap", 4000))
        self.warn_percent = float(config.get("cost.warn_at_percent", 80))
        self.fallback = str(config.get("cost.local_fallback", "ollama"))
        self._clock = clock
        self.state_path = Path(state_path or config.get("cost.state_file") or "state/cost_state.json")
        self._persist = persist
        self.state = self._load()

    @staticmethod
    def _month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _load(self) -> dict:
        default = {"month": self._month(), "spent_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "warned": False}
        if not self.state_path.exists():
            return default
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Corrupt cost state at {self.state_path}: {exc}") from exc
        if state.get("month") != default["month"]:
            return default
        return {**default, **state}

    def _save(self) -> None:
        if not self._persist:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
        self.state_path.chmod(0o600)

    def authorize(self, *, provider: str, local: bool, estimated_tokens: int | None = None) -> BudgetDecision:
        if local:
            return BudgetDecision(True, "local inference is not charged against cloud budget")
        if not self.enabled or self.budget <= 0:
            return BudgetDecision(
                False,
                "cloud disabled (budget is $0)",
                fallback=self.fallback,
            )
        if estimated_tokens is not None and estimated_tokens > self.token_cap:
            return BudgetDecision(
                False,
                f"request exceeds per-request token cap of {self.token_cap}",
                fallback=self.fallback,
            )
        spent = float(self.state["spent_usd"])
        if spent >= self.budget:
            return BudgetDecision(False, "monthly budget exhausted", fallback=self.fallback)
        warning = None
        if spent >= self.budget * self.warn_percent / 100:
            warning = f"cloud spend has reached {self.warn_percent}% of the monthly budget"
            self.state["warned"] = True
            self._save()
        return BudgetDecision(True, "cloud call within budget", warning=warning)

    def record(self, *, provider: str, local: bool, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> dict | None:
        """Record usage. Returns a warning event when a threshold is crossed."""
        self.state["tokens_in"] += int(tokens_in)
        self.state["tokens_out"] += int(tokens_out)
        event = None
        if not local:
            previous = float(self.state["spent_usd"])
            self.state["spent_usd"] = previous + max(0.0, float(cost_usd))
            now = float(self.state["spent_usd"])
            if previous < self.budget <= now:
                event = {
                    "type": "budget_hard_stop",
                    "message": "monthly cloud budget reached; future cloud calls fall back to local",
                    "spent_usd": now,
                }
            elif self.budget > 0 and previous < self.budget * self.warn_percent / 100 <= now:
                event = {
                    "type": "budget_warning",
                    "message": f"cloud spend crossed {self.warn_percent}% of the monthly budget",
                    "spent_usd": now,
                }
                self.state["warned"] = True
        self._save()
        return event

    def status(self) -> dict:
        spent = float(self.state["spent_usd"])
        return {
            "enabled": self.enabled,
            "monthly_budget_usd": self.budget,
            "spent_usd": round(spent, 6),
            "remaining_usd": round(max(0.0, self.budget - spent), 6),
            "warn_at_percent": self.warn_percent,
            "percent_used": round(100 * spent / self.budget, 2) if self.budget else 0.0,
            "token_cap": self.token_cap,
            "tokens_in": self.state["tokens_in"],
            "tokens_out": self.state["tokens_out"],
            "local_fallback": self.fallback,
        }
