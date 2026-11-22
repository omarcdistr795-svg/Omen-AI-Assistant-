"""Provider construction, configuration-driven swapping and model fallback."""

from __future__ import annotations

from typing import Any, Callable, Iterator

from .base import AdapterError, estimate_tokens
from .ollama import OllamaAdapter
from .openai_compatible import OpenAICompatibleAdapter

_ADAPTERS = {
    "openai_compatible": OpenAICompatibleAdapter,
    "ollama": OllamaAdapter,
}


def build_adapter(name: str, config: Any) -> Any:
    settings = (config.get("providers.adapters") or {}).get(name)
    if settings is None:
        raise AdapterError(f"Provider {name!r} is not configured", provider=name)
    kind = str(settings.get("type") or "")
    cls = _ADAPTERS.get(kind)
    if cls is None:
        raise AdapterError(f"Unsupported provider type {kind!r}", provider=name)
    return cls(name, dict(settings))


def build_adapters(config: Any) -> dict[str, Any]:
    return {name: build_adapter(name, config) for name in (config.get("providers.adapters") or {})}


class ProviderRouter:
    """Selects providers by config, enforces budget, and falls back on errors."""

    def __init__(
        self,
        config: Any,
        *,
        adapters: dict[str, Any] | None = None,
        budget: Any | None = None,
        audit: Any | None = None,
        notice: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters if adapters is not None else build_adapters(config)
        self.budget = budget
        self.audit = audit
        self.notice = notice or (lambda _message: None)
        self.order = [config.get("providers.default")] + list(config.get("providers.fallback") or [])
        for name in self.order:
            if name not in self.adapters:
                raise AdapterError(f"Configured provider {name!r} has no adapter", provider=name)

    def _candidates(self, messages: list[dict], estimated: int) -> list[tuple[str, Any]]:
        candidates: list[tuple[str, Any]] = []
        for name in dict.fromkeys(self.order):
            adapter = self.adapters[name]
            if self.budget is not None:
                decision = self.budget.authorize(
                    provider=name,
                    local=bool(getattr(adapter, "local", False)),
                    estimated_tokens=estimated,
                )
                if not decision.allowed:
                    message = f"{name} unavailable: {decision.reason}"
                    if decision.fallback:
                        message += f"; using local fallback {decision.fallback}"
                    self.notice(message)
                    self._log_budget_event(name, decision.reason)
                    continue
                if decision.warning:
                    self.notice(f"Cost warning: {decision.warning}")
                    self._log_budget_event(name, decision.warning)
            candidates.append((name, adapter))
        if not candidates:
            raise AdapterError("no provider available after budget checks", provider="router")
        return candidates

    def _log_budget_event(self, provider: str, message: str) -> None:
        if self.audit is None:
            return
        self.audit.append(
            {
                "event_type": "cost_control",
                "tool": "provider_router",
                "target": provider,
                "decision": "blocked" if "unavailable" in message else "allowed",
                "status": "warning",
                "summary": message,
                "provider": provider,
            }
        )

    def _record_usage(self, name: str, result: dict, messages: list[dict]) -> None:
        if self.budget is None:
            return
        adapter = self.adapters[name]
        usage = result.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or usage.get("prompt_eval_count") or 0)
        tokens_out = int(usage.get("completion_tokens") or usage.get("eval_count") or 0)
        if tokens_in == 0 and tokens_out == 0:
            tokens_in = int(result.get("estimated_tokens") or estimate_tokens(messages))
        event = self.budget.record(
            provider=name,
            local=bool(getattr(adapter, "local", False)),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=float(usage.get("cost_usd") or 0.0),
        )
        if self.audit is not None:
            self.audit.append(
                {
                    "event_type": "cost_usage",
                    "tool": "provider_router",
                    "target": name,
                    "status": "ok",
                    "decision": "allowed",
                    "provider": name,
                    "model": self.adapters[name].model,
                    "tokens": {"in": tokens_in, "out": tokens_out},
                    "cost_usd": float(usage.get("cost_usd") or 0.0),
                    "summary": f"provider {name} token usage recorded",
                }
            )
        if event is not None:
            self.notice(f"Cost control: {event['message']}")
            self._log_budget_event(name, event["message"])

    def complete(self, messages: list[dict], *, stream: bool = False, **kwargs) -> Any:
        candidates = self._candidates(messages, estimate_tokens(messages))
        if stream:
            return self._stream(messages, candidates, **kwargs)
        errors: list[str] = []
        for name, adapter in candidates:
            try:
                result = adapter.complete(messages, stream=False, **kwargs)
            except AdapterError as exc:
                errors.append(str(exc))
                self._log_failure(name, str(exc))
                continue
            self._record_usage(name, result, messages)
            return result
        raise AdapterError("all providers failed: " + " | ".join(errors), provider="router")

    def _stream(self, messages: list[dict], candidates: list[tuple[str, Any]], **kwargs) -> Iterator[str]:
        errors: list[str] = []
        for name, adapter in candidates:
            emitted = False
            try:
                for delta in adapter.complete(messages, stream=True, **kwargs):
                    emitted = True
                    yield delta
                return
            except AdapterError as exc:
                errors.append(str(exc))
                self._log_failure(name, str(exc))
                if emitted:
                    raise  # never duplicate a partially streamed answer
                continue
        raise AdapterError("all providers failed: " + " | ".join(errors), provider="router")

    def _log_failure(self, name: str, message: str) -> None:
        if self.audit is None:
            return
        self.audit.append(
            {
                "event_type": "provider_fallback",
                "tool": "provider_router",
                "target": name,
                "status": "error",
                "decision": "allowed",
                "error": message,
                "summary": f"provider {name} failed; trying fallback",
            }
        )

    def status(self) -> dict:
        return {
            "order": list(dict.fromkeys(self.order)),
            "adapters": [adapter.health() for name, adapter in self.adapters.items()],
            "budget": self.budget.status() if self.budget is not None else None,
        }
