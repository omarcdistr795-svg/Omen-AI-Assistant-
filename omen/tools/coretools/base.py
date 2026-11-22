"""Shared helpers for Phase 3 tools: path safety, validation and dry runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...memory.secret_rejection import validate_no_secrets
from ...safety.redaction import redact_value


class CoreToolError(ValueError):
    """A Phase 3 tool rejected its arguments or could not complete safely."""


class PathPolicyError(CoreToolError):
    """A path escaped the configured allowed roots or violated a size limit."""


def truncate(text: str, limit: int) -> str:
    """Cap tool output without hiding the fact that it was capped."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} characters]"


class PathPolicy:
    """Confine file access to explicit roots, including through symlinks."""

    def __init__(
        self,
        roots: list[str] | tuple[str, ...] | None,
        *,
        base: Path | str | None = None,
        max_file_bytes: int = 1_048_576,
        max_output_chars: int = 20_000,
    ) -> None:
        anchor = Path(base).resolve() if base is not None else Path.cwd().resolve()
        candidates = list(roots or ["."])
        self.roots: tuple[Path, ...] = tuple(
            (anchor / str(item)).resolve() if not Path(str(item)).is_absolute() else Path(str(item)).resolve()
            for item in candidates
        )
        if not self.roots:
            raise CoreToolError("At least one allowed root is required")
        self.max_file_bytes = int(max_file_bytes)
        self.max_output_chars = int(max_output_chars)
        if self.max_file_bytes <= 0:
            raise CoreToolError("max_file_bytes must be positive")

    def resolve(self, value: Any, *, must_exist: bool = True) -> Path:
        raw = str(value or "").strip()
        if not raw:
            raise PathPolicyError("A path is required")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        # resolve() follows symlinks; checking the final target prevents escape
        # through a symlink inside an allowed root.
        resolved = candidate.resolve(strict=False)
        if not any(resolved == root or root in resolved.parents for root in self.roots):
            raise PathPolicyError(f"Path is outside the allowed roots: {raw}")
        if must_exist and not resolved.exists():
            raise PathPolicyError(f"Path does not exist: {raw}")
        if resolved.exists() and resolved.is_file() and resolved.stat().st_size > self.max_file_bytes:
            raise PathPolicyError(f"File exceeds {self.max_file_bytes} byte limit: {raw}")
        return resolved

    def read_text(self, path: Any) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise PathPolicyError(f"Not a readable file: {target}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PathPolicyError(f"File is not UTF-8 text: {target}") from exc
        return truncate(text, self.max_output_chars)


class CoreTool:
    """Small base class implementing the Phase 1 Tool protocol."""

    name: str = ""
    description: str = ""
    permission_class: str = "read_only"
    _schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def schema(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._schema))

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        """Plans only: never touches disk, network, processes, or the desktop."""
        return {
            "tool": self.name,
            "dry_run": True,
            "would_call": redact_value(kwargs),
            "permission_class": self.permission_class,
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError(f"{self.name} must implement execute()")

    @staticmethod
    def require(params: dict[str, Any], *keys: str) -> None:
        missing = [key for key in keys if params.get(key) in (None, "")]
        if missing:
            raise CoreToolError(f"Missing required parameter(s): {', '.join(missing)}")

    @staticmethod
    def clean_data(payload: dict[str, Any] | None) -> dict[str, Any]:
        """Never persist secrets in Phase 3 local storage."""
        data = dict(payload or {})
        validate_no_secrets(json.dumps(data, ensure_ascii=False), data)
        return data

    @staticmethod
    def text(value: Any) -> str:
        return str(value if value is not None else "")
