"""OpenAI-compatible adapter (also covers LM Studio via base_url override)."""

from __future__ import annotations

import json
from typing import Any, Iterator

from .base import AdapterError, estimate_tokens, post_json, post_stream


class OpenAICompatibleAdapter:
    """Implements ProviderAdapter for /v1/chat/completions-style servers."""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        self.model = str(config.get("model") or "")
        self.api_key = str(config.get("api_key") or "")
        self.timeout = float(config.get("timeout_seconds", 30))
        self.local = bool(config.get("local", False))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    def _payload(self, messages: list[dict], stream: bool, **kwargs) -> dict:
        return {"model": self.model, "messages": messages, "stream": stream, **kwargs}

    def _endpoint(self) -> str:
        if not self.base_url:
            raise AdapterError("base_url is not configured", provider=self.name)
        return f"{self.base_url}/chat/completions"

    def complete(self, messages: list[dict], *, stream: bool = False, **kwargs) -> Any:
        if stream:
            return self._stream(messages, **kwargs)
        data = post_json(
            self._endpoint(),
            self._payload(messages, False, **kwargs),
            headers=self._headers(),
            timeout=self.timeout,
            provider=self.name,
        )
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"unexpected response shape: {exc}", provider=self.name) from exc
        return {
            "content": content,
            "provider": self.name,
            "model": self.model,
            "usage": data.get("usage") or {},
            "estimated_tokens": estimate_tokens(messages),
        }

    def _stream(self, messages: list[dict], **kwargs) -> Iterator[str]:
        for data in post_stream(
            self._endpoint(),
            self._payload(messages, True, **kwargs),
            headers=self._headers(),
            timeout=self.timeout,
            provider=self.name,
        ):
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"].get("content") or ""
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise AdapterError(f"unexpected stream chunk: {exc}", provider=self.name) from exc
            if delta:
                yield delta

    def health(self) -> dict:
        return {
            "name": self.name,
            "type": "openai_compatible",
            "model": self.model,
            "local": self.local,
            "configured": bool(self.base_url and self.model),
            "api_key_present": bool(self.api_key),
            "reachable": False,
            "note": "reachability is checked by a real completion, not by health()",
        }
