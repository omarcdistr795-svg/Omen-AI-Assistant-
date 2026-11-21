"""Secret rejection validation for Omen Memory.

Memory refuses to store API keys, passwords, tokens, private keys, or other
credentials in memory, prompts, transcripts, or logs.
"""

from __future__ import annotations

import re
from typing import Any


class SecretRejectedError(ValueError):
    """Raised when memory operations attempt to store secrets."""


# Known secret patterns that must be rejected
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "private key block",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "API key (sk-*)"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key (AKIA*)"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "GitHub token (gh*_)"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "JWT token",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"), "Bearer authorization token"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential|auth_token|access_token|private_key)\s*[:=]\s*[\"']?[^\s,;\"'}]{4,}[\"']?"
        ),
        "credential assignment",
    ),
    (
        re.compile(
            r"(?i)\b(postgres|postgresql|mysql|mongodb|redis):\/\/[A-Za-z0-9._%+-]+:[^\s@]+@[A-Za-z0-9.-]+"
        ),
        "database URI with password",
    ),
)

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "credential",
        "token",
        "private_key",
        "auth_token",
        "access_token",
    }
)


def detect_secret(text: str) -> str | None:
    """Return description of detected secret, or None if clean."""
    if not isinstance(text, str) or not text.strip():
        return None
    for pattern, description in _SECRET_PATTERNS:
        if pattern.search(text):
            return description
    return None


def validate_no_secrets(text: str, metadata: dict[str, Any] | None = None) -> None:
    """Validate that text and metadata contain no secrets.

    Raises SecretRejectedError if any secret pattern or sensitive field is detected.
    """
    secret = detect_secret(text)
    if secret:
        raise SecretRejectedError(f"Memory refuses to store secrets: detected {secret}")

    if metadata:
        _scan_metadata_for_secrets(metadata)


def _scan_metadata_for_secrets(data: Any, path: str = "metadata") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            key_str = str(key).lower()
            current_path = f"{path}.{key}"
            if any(hint in key_str for hint in _SENSITIVE_FIELD_NAMES):
                # Allow safe summary token keys
                if key_str not in ("token_cap", "tokens", "token_usage"):
                    raise SecretRejectedError(
                        f"Memory refuses to store sensitive metadata field: {current_path}"
                    )
            _scan_metadata_for_secrets(value, current_path)
    elif isinstance(data, (list, tuple, set)):
        for idx, item in enumerate(data):
            _scan_metadata_for_secrets(item, f"{path}[{idx}]")
    elif isinstance(data, str):
        secret = detect_secret(data)
        if secret:
            raise SecretRejectedError(
                f"Memory refuses to store secrets in {path}: detected {secret}"
            )
