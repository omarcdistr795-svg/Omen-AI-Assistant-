"""Rollback plans for Phase 3 file, git and calendar changes."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ...memory.models import generate_safe_id
from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, PathPolicy

_HASH = re.compile(r"^[0-9a-f]{7,40}$")


class RollbackStore:
    """Append-only plan records stored beside the local Phase 3 data."""

    def __init__(self, config: Any, root: Path | str) -> None:
        relative = str(config.get("core_tools.storage.local_db", "state/phase3/local.db"))
        path = Path(relative)
        self.path = (Path(root).resolve() / path) if not path.is_absolute() else path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS phase3_rollback (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    undone_at TEXT
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record(self, kind: str, tool: str, target: str, payload: dict[str, Any]) -> str:
        allowed = {
            "file_remove",
            "file_restore",
            "file_restore_move",
            "record_restore",
            "record_remove",
            "git_commit",
        }
        if kind not in allowed:
            raise CoreToolError(f"Unknown rollback plan kind {kind!r}")
        plan_id = generate_safe_id("undo")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO phase3_rollback(id, kind, tool, target, payload, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (plan_id, kind, tool, str(target), json.dumps(payload, ensure_ascii=False), "pending", now),
            )
        return plan_id

    def get(self, plan_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM phase3_rollback WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise CoreToolError(f"No rollback plan with id {plan_id!r}")
        return {
            "id": row["id"],
            "kind": row["kind"],
            "tool": row["tool"],
            "target": row["target"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "undone_at": row["undone_at"],
        }

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM phase3_rollback ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "tool": row["tool"],
                "target": row["target"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def mark_undone(self, plan_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE phase3_rollback SET status='undone', undone_at=? WHERE id=? AND status='pending'",
                (now, plan_id),
            )
            if cursor.rowcount == 0:
                raise CoreToolError(f"Rollback plan {plan_id!r} is not pending")


class RollbackPlan(CoreTool):
    name = "rollback.plan"
    description = "Inspect a rollback plan or list recent plans (read-only)."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
        "additionalProperties": False,
    }

    def __init__(self, store: RollbackStore) -> None:
        self.store = store

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("plan_id"):
            return {"plan": redact_value(self.store.get(self.text(kwargs.get("plan_id"))))}
        return {"plans": redact_value(self.store.list(int(kwargs.get("limit") or 50)))}


class RollbackUndo(CoreTool):
    name = "rollback.undo"
    description = "Undo a recorded file, calendar or git change (requires approval)."
    permission_class = "high_risk"
    _schema = {
        "type": "object",
        "properties": {"plan_id": {"type": "string", "minLength": 1}},
        "required": ["plan_id"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        store: RollbackStore,
        *,
        policy: PathPolicy,
        local_store: Any,
        quarantine_dir: Path,
        git_roots: tuple[Path, ...],
        connectors: Any | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.local_store = local_store
        self.quarantine_dir = quarantine_dir
        self.git_roots = git_roots
        self._connector = connectors

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "plan_id")
        plan = self.store.get(self.text(kwargs.get("plan_id")))
        return {
            "tool": self.name,
            "dry_run": True,
            "would_undo": plan["kind"],
            "target": redact_value(plan["target"]),
            "status": plan["status"],
            "side_effects": "none",
        }

    def _safe_path(self, value: Any, *, must_exist: bool = False) -> Path:
        path = Path(str(value))
        resolved = self.policy.resolve(path, must_exist=must_exist)
        if resolved != path.resolve(strict=False):
            raise CoreToolError("Rollback path changed during resolution; refusing to act")
        return resolved

    def _git(self, repo: Path, args: list[str]) -> str:
        resolved = repo.resolve()
        if not any(resolved == root or root in resolved.parents for root in self.git_roots):
            raise CoreToolError("Rollback git repository is outside the allowed roots")
        completed = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=str(resolved),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise CoreToolError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
        return completed.stdout.strip()

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "plan_id")
        plan = self.store.get(self.text(kwargs.get("plan_id")))
        if plan["status"] != "pending":
            raise CoreToolError(f"Rollback plan is already {plan['status']}")
        kind = plan["kind"]
        payload = plan["payload"]
        detail: dict[str, Any] = {"plan_id": plan["id"], "kind": kind, "target": redact_value(plan["target"])}

        if kind == "file_remove":
            target = self._safe_path(payload["path"], must_exist=True)
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            moved = self.quarantine_dir / f"removed_{target.name}"
            shutil.move(str(target), str(moved))
            detail["quarantine"] = str(moved)
        elif kind == "file_restore":
            target = self._safe_path(payload["path"], must_exist=True)
            backup = self._safe_path(payload["backup"], must_exist=True)
            shutil.copy2(backup, target)
            detail["restored"] = str(target)
        elif kind == "file_restore_move":
            target = self._safe_path(payload["path"])
            source = self._safe_path(payload["quarantine"], must_exist=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            detail["restored"] = str(target)
        elif kind in ("record_restore", "record_remove"):
            connector_name = payload.get("connector")
            if connector_name:
                connector = self._connector.get_by_name(connector_name)
                if connector is None:
                    raise CoreToolError(f"Rollback connector {connector_name!r} is no longer available")
                if kind == "record_restore":
                    record = payload["record"]
                    connector.call("restore", {"domain": payload.get("domain"), "record": record})
                    detail["restored"] = record.get("id")
                else:
                    connector.call("remove", {"domain": payload.get("domain"), "id": payload["id"]})
                    detail["removed"] = payload["id"]
            elif kind == "record_restore":
                record = payload["record"]
                self.local_store.restore(str(record.get("domain") or payload.get("domain")), record)
                detail["restored"] = record.get("id")
            else:
                self.local_store.delete(str(payload["domain"]), str(payload["id"]))
                detail["removed"] = payload["id"]
        elif kind == "git_commit":
            repo = Path(str(payload["repo"]))
            before = str(payload["before_head"])
            expected = str(payload["commit"])
            if not _HASH.fullmatch(before) or not _HASH.fullmatch(expected):
                raise CoreToolError("Rollback commit identifiers are invalid")
            if self._git(repo, ["rev-parse", "HEAD"]) != expected:
                raise CoreToolError("HEAD moved since the commit; refusing to reset")
            self._git(repo, ["reset", "--soft", before])
            detail["reset_to"] = before
        else:  # pragma: no cover - guarded by RollbackStore.record
            raise CoreToolError(f"Cannot undo plan kind {kind!r}")

        self.store.mark_undone(plan["id"])
        detail["status"] = "undone"
        detail["rollback"] = "undo is one-shot; plan is now marked undone"
        return detail
