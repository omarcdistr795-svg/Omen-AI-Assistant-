"""Local Ollama adapter."""

from __future__ import annotations

import json
from typing import Any, Iterator

from .base import AdapterError, estimate_tokens, post_json, post_lines


class OllamaAdapter:
    """Implements ProviderAdapter against Ollama's /api/chat endpoint."""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.base_url = str(config.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        self.model = str(config.get("model") or "")
        self.timeout = float(config.get("timeout_seconds", 30))
        self.local = True

    def _endpoint(self) -> str:
        return f"{self.base_url}/api/chat"

    def _payload(self, messages: list[dict], stream: bool, **kwargs) -> dict:
        return {"model": self.model, "messages": messages, "stream": stream, **kwargs}

    def complete(self, messages: list[dict], *, stream: bool = False, **kwargs) -> Any:
        if stream:
            return self._stream(messages, **kwargs)
        data = post_json(
            self._endpoint(),
            self._payload(messages, False, **kwargs),
            headers={},
            timeout=self.timeout,
            provider=self.name,
        )
        try:
            content = data["message"]["content"] or ""
        except (KeyError, TypeError) as exc:
            raise AdapterError(f"unexpected response shape: {exc}", provider=self.name) from exc
        return {
            "content": content,
            "provider": self.name,
            "model": self.model,
            "usage": {"prompt_eval_count": data.get("prompt_eval_count", 0), "eval_count": data.get("eval_count", 0)},
            "estimated_tokens": estimate_tokens(messages),
        }

    def _stream(self, messages: list[dict], **kwargs) -> Iterator[str]:
        for line in post_lines(
            self._endpoint(),
            self._payload(messages, True, **kwargs),
            headers={},
            timeout=self.timeout,
            provider=self.name,
        ):
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdapterError(f"unexpected stream chunk: {exc}", provider=self.name) from exc
            delta = (chunk.get("message") or {}).get("content") or ""
            if delta:
                yield delta
            if chunk.get("done"):
                return

    def health(self) -> dict:
        return {
            "name": self.name,
            "type": "ollama",
            "model": self.model,
            "local": True,
            "configured": bool(self.base_url and self.model),
            "api_key_present": False,
            "reachable": False,
            "note": "reachability is checked by a real completion, not by health()",
        }
