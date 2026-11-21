"""Secret and personal-data redaction applied before logging or model context."""

from __future__ import annotations

import re
from typing import Any

SAFE_TOKEN_KEYS = frozenset({
    "token_cap", "tokens", "tokens_in", "tokens_out", "token_usage",
    "estimated_tokens", "prompt_tokens", "completion_tokens",
    "token_id", "event_id", "trace_id", "session_id", "job_id", "record_id",
})

SECRET_KEY_HINTS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization",
    "private_key",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[PRIVATE_KEY_REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[API_KEY_REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY_REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "[GITHUB_TOKEN_REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[JWT_REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"), "Bearer [TOKEN_REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential)\s*[:=]\s*[\"']?[^\s,;\"'}]{4,}[\"']?"), r"\1=[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)"), "[PHONE_REDACTED]"),
)


_SAFE_ID = re.compile(r"[a-f0-9]{32}")


def _protect_safe_ids(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}
    def repl(match: re.Match[str]) -> str:
        key = f"__OMEN_SAFE_ID_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key
    return _SAFE_ID.sub(repl, text), placeholders


def _restore_safe_ids(text: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def redact_text(text: str, *, personal_data: bool = True) -> str:
    """Redact known secret shapes and, optionally, emails/phone numbers."""
    result, placeholders = _protect_safe_ids(text)
    for index, (pattern, replacement) in enumerate(_PATTERNS):
        if not personal_data and index >= len(_PATTERNS) - 2:
            continue
        result = pattern.sub(replacement, result)
    return _restore_safe_ids(result, placeholders)


def redact_value(value: Any, *, personal_data: bool = True) -> Any:
    """Recursively redact mapping keys and string values."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text not in SAFE_TOKEN_KEYS and any(hint in key_text.lower() for hint in SECRET_KEY_HINTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_value(item, personal_data=personal_data)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_value(item, personal_data=personal_data) for item in value]
    if isinstance(value, str):
        return redact_text(value, personal_data=personal_data)
    return value
