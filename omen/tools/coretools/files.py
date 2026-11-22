"""File tools: read, search, summarise, draft, update and soft-delete.

All writes require approval through their permission class and create a rollback
plan. Paths are confined to configured roots, including through symlinks.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, PathPolicy, truncate

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "state"}


class _FileTool(CoreTool):
    def __init__(self, policy: PathPolicy, rollback: Any, drafts_dir: Path, quarantine_dir: Path) -> None:
        self.policy = policy
        self.rollback = rollback
        self.drafts_dir = drafts_dir
        self.quarantine_dir = quarantine_dir

    @staticmethod
    def _stamp(name: str) -> str:
        from datetime import datetime, timezone

        return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{name}"


class FileRead(_FileTool):
    name = "files.read"
    description = "Read a UTF-8 text file inside the allowed roots (redacted in output)."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "path")
        target = self.policy.resolve(kwargs["path"])
        if not target.is_file():
            raise CoreToolError(f"Not a file: {target}")
        text = self.policy.read_text(target)
        lines = text.splitlines()
        start = max(1, int(kwargs.get("start_line") or 1))
        count = max(1, min(int(kwargs.get("max_lines") or 2000), 2000))
        selected = lines[start - 1 : start - 1 + count]
        return {
            "path": str(target),
            "content": redact_value(truncate("\n".join(selected), self.policy.max_output_chars)),
            "lines": len(selected),
            "total_lines": len(lines),
        }


class FileSearch(_FileTool):
    name = "files.search"
    description = "Search text files in the allowed roots; returns paths and matching lines."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "regex": {"type": "boolean"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "query")
        root = self.policy.resolve(kwargs.get("path") or self.policy.roots[0], must_exist=True)
        base = root if root.is_dir() else root.parent
        pattern = self.text(kwargs.get("query"))
        use_regex = bool(kwargs.get("regex"))
        try:
            matcher = re.compile(pattern if use_regex else re.escape(pattern), re.I)
        except re.error as exc:
            raise CoreToolError(f"Invalid search pattern: {exc}") from exc
        limit = max(1, min(int(kwargs.get("limit") or 20), 100))
        matches: list[dict[str, Any]] = []
        for path in sorted(base.rglob("*")):
            if len(matches) >= limit:
                break
            if not path.is_file() or path.is_symlink() or path.stat().st_size > self.policy.max_file_bytes:
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in path.relative_to(base).parts[:-1]):
                continue
            if not any(root in path.resolve(strict=False).parents or path.resolve(strict=False) == root for root in self.policy.roots):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if matcher.search(line):
                    matches.append(
                        {
                            "path": str(path),
                            "line": number,
                            "text": redact_value(truncate(line.strip(), 300)),
                        }
                    )
                    if len(matches) >= limit:
                        break
        return {"query": pattern, "matches": matches, "truncated": len(matches) >= limit}


class FileSummarise(_FileTool):
    name = "files.summarise"
    description = "Extractively summarise an allowed text, code, CSV or JSON file."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "max_sentences": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "path")
        target = self.policy.resolve(kwargs["path"])
        raw = self.policy.read_text(target)
        if target.suffix.lower() == ".json":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CoreToolError(f"Invalid JSON file: {exc}") from exc
            content = json.dumps(parsed, ensure_ascii=False, indent=2)[: self.policy.max_output_chars]
        elif target.suffix.lower() == ".csv":
            lines = raw.splitlines()
            content = "\n".join(lines[:200])
        else:
            content = raw
        sentences = [part.strip() for part in _SENTENCE.split(content) if part.strip()]
        if not sentences:
            sentences = [line.strip() for line in content.splitlines() if line.strip()]
        count = max(1, min(int(kwargs.get("max_sentences") or 5), 12))
        chosen = sentences[:count]
        return {
            "path": str(target),
            "summary": redact_value(" ".join(chosen)),
            "sentences": len(chosen),
            "source": str(target),
            "method": "extractive",
        }


class FileDraft(_FileTool):
    name = "files.draft"
    description = "Draft a new UTF-8 file in the drafts directory (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "minLength": 1, "maxLength": 160},
            "content": {"type": "string", "maxLength": 100000},
        },
        "required": ["filename", "content"],
        "additionalProperties": False,
    }

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        filename = Path(self.text(kwargs.get("filename"))).name
        return {
            "tool": self.name,
            "dry_run": True,
            "would_create": str(self.drafts_dir / filename),
            "bytes": len(self.text(kwargs.get("content")).encode("utf-8")),
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "filename", "content")
        filename = Path(self.text(kwargs.get("filename"))).name
        if filename in ("", ".", "..") or Path(filename).is_absolute():
            raise CoreToolError("filename must be a simple file name")
        target = self.drafts_dir / filename
        if target.exists():
            raise CoreToolError(f"Draft already exists; use files.update: {target}")
        content = self.clean_data({"content": self.text(kwargs.get("content"))})["content"]
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        plan = self.rollback.record("file_remove", self.name, str(target), {"path": str(target)})
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "rollback_plan_id": plan,
            "rollback": "undo moves the draft into quarantine",
        }


class FileUpdate(_FileTool):
    name = "files.update"
    description = "Update an existing file inside the allowed roots, keeping a backup (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string", "maxLength": 100000},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tool": self.name,
            "dry_run": True,
            "would_update": self.text(kwargs.get("path")),
            "backup": "created in quarantine before writing",
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "path", "content")
        target = self.policy.resolve(kwargs["path"])
        if not target.is_file():
            raise CoreToolError(f"Not a file: {target}")
        content = self.clean_data({"content": self.text(kwargs.get("content"))})["content"]
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        backup = self.quarantine_dir / self._stamp(f"{target.name}.backup")
        shutil.copy2(target, backup)
        try:
            target.write_text(str(content), encoding="utf-8")
        except Exception:
            shutil.copy2(backup, target)
            raise
        plan = self.rollback.record(
            "file_restore", self.name, str(target), {"path": str(target), "backup": str(backup)}
        )
        return {
            "path": str(target),
            "backup": str(backup),
            "rollback_plan_id": plan,
            "rollback": "undo restores the backed-up version",
        }


class FileDelete(_FileTool):
    name = "files.delete"
    description = "Soft-delete one allowed file or directory by moving it into quarantine (requires approval)."
    permission_class = "high_risk"
    _schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tool": self.name,
            "dry_run": True,
            "would_move_to_quarantine": self.text(kwargs.get("path")),
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "path")
        raw = self.text(kwargs["path"])
        if any(char in raw for char in "*?[]"):
            raise CoreToolError("Wildcard paths are not accepted; provide one exact path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.policy.roots[0] / candidate
        # Reject symlink aliases, including a symlinked final target. PathPolicy
        # still performs the authoritative allowed-root/escape check below.
        cursor = candidate
        while cursor != cursor.parent:
            if cursor.is_symlink():
                raise CoreToolError(f"Symlink paths cannot be quarantined: {cursor}")
            cursor = cursor.parent
        target = self.policy.resolve(raw)
        if not target.is_file() and not target.is_dir():
            raise CoreToolError(f"Not a regular file or directory: {target}")
        if target in self.policy.roots:
            raise CoreToolError("An allowed root cannot be quarantined")
        protected = (self.drafts_dir.resolve(), self.quarantine_dir.resolve())
        if any(target == path or path in target.parents or target in path.parents for path in protected):
            raise CoreToolError("Phase 3 draft/quarantine paths and their parents are managed internally")
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = self.quarantine_dir / self._stamp(target.name)
        if destination.exists():
            raise CoreToolError("A quarantine entry with this generated name already exists")
        is_directory = target.is_dir()
        try:
            # A directory is quarantined with one same-filesystem rename. Do
            # not fall back to copytree/remove behavior across mount points.
            target.rename(destination) if is_directory else shutil.move(str(target), str(destination))
        except OSError as exc:
            if is_directory:
                raise CoreToolError(f"Cannot quarantine directory without copying or deleting its contents: {exc}") from exc
            raise
        try:
            plan = self.rollback.record(
                "file_restore_move", self.name, str(target), {"path": str(target), "quarantine": str(destination)}
            )
        except Exception:
            destination.rename(target) if is_directory else shutil.move(str(destination), str(target))
            raise
        return {
            "deleted": str(target),
            "quarantine": str(destination),
            "rollback_plan_id": plan,
            "rollback": "undo moves the quarantined item back",
        }


class FileList(_FileTool):
    name = "files.list"
    description = "List files in an allowed directory without following symlinks."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        target = self.policy.resolve(kwargs.get("path") or self.policy.roots[0])
        if not target.is_dir():
            raise CoreToolError(f"Not a directory: {target}")
        limit = max(1, min(int(kwargs.get("limit") or 50), 200))
        entries = [
            {"name": item.name, "path": str(item), "type": "dir" if item.is_dir() else "file"}
            for item in sorted(target.iterdir())[:limit]
        ]
        return {"path": str(target), "entries": redact_value(entries)}
