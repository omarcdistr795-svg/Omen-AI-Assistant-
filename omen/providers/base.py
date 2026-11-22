"""HTTP and streaming helpers shared by provider adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

from ..safety.redaction import redact_text


class AdapterError(RuntimeError):
    """Provider failure. Never swallowed by the router without a fallback."""

    def __init__(self, message: str, *, provider: str = "unknown", status: int | None = None) -> None:
        self.provider = provider
        self.status = status
        super().__init__(f"[{provider}] {redact_text(message)}")


def post_json(url: str, payload: dict, *, headers: dict[str, str], timeout: float, provider: str) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AdapterError(f"HTTP {exc.code}: {detail}", provider=provider, status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"connection failed: {exc}", provider=provider) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"invalid JSON response: {exc}", provider=provider) from exc


def post_stream(url: str, payload: dict, *, headers: dict[str, str], timeout: float, provider: str) -> Iterator[str]:
    """Yield data payloads from an SSE stream."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                yield data
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AdapterError(f"HTTP {exc.code}: {detail}", provider=provider, status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"connection failed: {exc}", provider=provider) from exc


def post_lines(url: str, payload: dict, *, headers: dict[str, str], timeout: float, provider: str) -> Iterator[str]:
    """Yield newline-delimited JSON responses (Ollama-style streaming)."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    yield line
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AdapterError(f"HTTP {exc.code}: {detail}", provider=provider, status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"connection failed: {exc}", provider=provider) from exc


def estimate_tokens(messages: list[dict]) -> int:
    """Conservative char/4 estimate; provider usage supersedes it when returned."""
    return sum(len(str(message.get("content", ""))) // 4 + 4 for message in messages)
