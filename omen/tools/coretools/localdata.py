"""Local-first notes, reminders, tasks and calendar storage (Phase 3).

Data lives in a small SQLite database under ``state/phase3/``. Optional
MCP-style connectors can be attached later without changing these tool
interfaces; the default is always local storage.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ...memory.models import generate_safe_id, now_utc_iso
from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, truncate

DOMAINS = ("notes", "reminders", "tasks", "calendar")
DEFAULT_STATUSES = {
    "notes": "active",
    "reminders": "pending",
    "tasks": "todo",
    "calendar": "scheduled",
}
TEXT_LIMIT = 12_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalDataStore:
    """Additive SQLite store; no Phase 1/2 file is modified."""

    def __init__(self, config: Any, root: Path | str) -> None:
        relative = str(config.get("core_tools.storage.local_db", "state/phase3/local.db"))
        path = Path(relative)
        self.path = (Path(root).resolve() / path) if not path.is_absolute() else path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS phase3_records (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_phase3_domain ON phase3_records(domain)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "domain": row["domain"],
            "status": row["status"],
            **json.loads(row["data"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _domain(domain: str) -> str:
        if domain not in DOMAINS:
            raise CoreToolError(f"Unknown local domain {domain!r}")
        return domain

    def create(self, domain: str, data: dict[str, Any]) -> dict[str, Any]:
        domain = self._domain(domain)
        record_id = generate_safe_id(domain[:-1])
        payload = dict(data)
        payload.pop("id", None)
        status = str(payload.pop("status", DEFAULT_STATUSES[domain]))
        now = _utc_now()
        payload.update({"id": record_id, "domain": domain, "status": status, "created_at": now, "updated_at": now})
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO phase3_records(id, domain, status, data, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (record_id, domain, status, json.dumps(payload, ensure_ascii=False), now, now),
            )
        return redact_value(payload)

    def get(self, domain: str, record_id: str) -> dict[str, Any]:
        domain = self._domain(domain)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM phase3_records WHERE id=? AND domain=?", (record_id, domain)
            ).fetchone()
        if row is None:
            raise CoreToolError(f"No {domain} record with id {record_id!r}")
        return redact_value(self._row(row))

    def find(self, domain: str, record_id: str) -> dict[str, Any] | None:
        try:
            return self.get(domain, record_id)
        except CoreToolError:
            return None

    def list(
        self,
        domain: str,
        *,
        status: str | None = None,
        query: str = "",
        limit: int = 50,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        domain = self._domain(domain)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM phase3_records WHERE domain=? ORDER BY updated_at DESC", (domain,)
            ).fetchall()
        results = [self._row(row) for row in rows if row["status"] != "deleted" and (status is None or row["status"] == status)]
        if query:
            needle = query.lower()
            results = [item for item in results if needle in json.dumps(item, ensure_ascii=False).lower()]
        if start:
            results = [item for item in results if str(item.get("start") or item.get("due_at") or "") >= start]
        if end:
            results = [item for item in results if str(item.get("start") or item.get("due_at") or "") <= end]
        return redact_value(results[: max(1, min(int(limit), 200))])

    def update(self, domain: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        domain = self._domain(domain)
        before = self.get(domain, record_id)
        payload = {key: value for key, value in before.items() if key not in ("id", "domain", "created_at", "updated_at")}
        payload.update(changes)
        payload.pop("id", None)
        payload.pop("domain", None)
        status = str(payload.pop("status", before["status"]))
        now = _utc_now()
        payload.update({"id": record_id, "domain": domain, "status": status, "created_at": before["created_at"], "updated_at": now})
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE phase3_records SET status=?, data=?, updated_at=? WHERE id=? AND domain=?",
                (status, json.dumps(payload, ensure_ascii=False), now, record_id, domain),
            )
            if cursor.rowcount == 0:
                raise CoreToolError(f"No {domain} record with id {record_id!r}")
        return {"after": redact_value(payload), "before": redact_value(before)}

    def delete(self, domain: str, record_id: str) -> dict[str, Any]:
        domain = self._domain(domain)
        before = self.get(domain, record_id)
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE phase3_records SET status='deleted', updated_at=? WHERE id=? AND domain=?",
                (now, record_id, domain),
            )
            if cursor.rowcount == 0:
                raise CoreToolError(f"No {domain} record with id {record_id!r}")
        return {"before": redact_value(before), "deleted_at": now}

    def restore(self, domain: str, record: dict[str, Any]) -> dict[str, Any]:
        domain = self._domain(domain)
        payload = dict(record)
        record_id = str(payload.pop("id"))
        status = str(payload.pop("status", DEFAULT_STATUSES[domain]))
        payload.pop("domain", None)
        now = _utc_now()
        payload.update({"id": record_id, "domain": domain, "status": status, "updated_at": now})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO phase3_records(id, domain, status, data, created_at, updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, data=excluded.data, updated_at=excluded.updated_at
                """,
                (
                    record_id,
                    domain,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    str(payload.get("created_at") or now),
                    now,
                ),
            )
        return redact_value(payload)

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT domain, COUNT(*) AS count FROM phase3_records GROUP BY domain").fetchall()
        return {
            "path": str(self.path),
            "counts": {row["domain"]: row["count"] for row in rows},
            "local_first": True,
        }


_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "query": {"type": "string", "maxLength": 300},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "start": {"type": "string"},
        "end": {"type": "string"},
    },
    "additionalProperties": False,
}

_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "maxLength": 20000},
        "description": {"type": "string", "maxLength": 20000},
        "start": {"type": "string"},
        "end": {"type": "string"},
        "due_at": {"type": "string"},
        "status": {"type": "string"},
        "priority": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "object"}},
        "timezone": {"type": "string"},
    },
    "required": ["title"],
    "additionalProperties": False,
}

_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string", "minLength": 1}},
    "required": ["id"],
    "additionalProperties": False,
}

_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "changes": {"type": "object"},
    },
    "required": ["id", "changes"],
    "additionalProperties": False,
}


class LocalRecordTool(CoreTool):
    """One registered tool per (domain, action), all on the same local store."""

    def __init__(
        self,
        domain: str,
        action: str,
        store: LocalDataStore,
        rollback: Any,
        *,
        permission_class: str | None = None,
        description: str | None = None,
        connector: Any | None = None,
    ) -> None:
        self.domain = domain
        self.action = action
        self.store = store
        self.rollback = rollback
        self.connector = connector
        self.name = f"{domain}.{action}"
        self.permission_class = permission_class or ("read_only" if action in ("get", "list", "search", "summarise") else "low_risk_write")
        self.description = description or f"{action.replace('_', ' ')} {domain} in local-first storage."
        self._schema = {
            "create": _CREATE_SCHEMA,
            "get": _ID_SCHEMA,
            "list": _LIST_SCHEMA,
            "search": {**_LIST_SCHEMA, "required": ["query"]},
            "update": _UPDATE_SCHEMA,
            "delete": _ID_SCHEMA,
            "complete": _ID_SCHEMA,
        }.get(action, _ID_SCHEMA)

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tool": self.name,
            "dry_run": True,
            "would": f"{self.action} a local {self.domain} record",
            "domain": self.domain,
            "params": redact_value(kwargs),
            "side_effects": "none",
        }

    def _external(self, response: Any, plan: str | None, rollback_note: str | None) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise CoreToolError("Connector must return a structured dict")
        output = dict(response)
        output.update(
            {
                "storage": self.connector.name,
                "external": bool(self.connector.external),
                "local_first": not bool(self.connector.external),
            }
        )
        if plan:
            output["rollback_plan_id"] = plan
            output["rollback"] = rollback_note
        return redact_value(output)

    def _execute_connector(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        action = self.action
        connector = self.connector
        params = self.clean_data(dict(kwargs)) if action in ("create", "update") else dict(kwargs)
        if action == "create":
            response = connector.call("create", params)
            record = response.get("record") or response
            record_id = str(record.get("id") or response.get("id") or "")
            if not record_id:
                raise CoreToolError("Connector did not return a record id")
            plan = self.rollback.record(
                "record_remove",
                self.name,
                record_id,
                {"connector": connector.name, "domain": self.domain, "id": record_id},
            )
            return self._external(response, plan, "undo asks the connector to remove the record")
        if action in ("update", "delete", "complete"):
            self.require(kwargs, "id")
            before_response = connector.call("get", {"id": self.text(kwargs.get("id"))})
            before = before_response.get("record") or before_response
            response = connector.call(action, params)
            plan = self.rollback.record(
                "record_restore",
                self.name,
                self.text(kwargs.get("id")),
                {"connector": connector.name, "domain": self.domain, "record": before},
            )
            return self._external(response, plan, "undo asks the connector to restore the prior record")
        return self._external(connector.call(action, params), None, None)

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = self.action
        if self.connector is not None:
            return self._execute_connector(kwargs)
        if action == "create":
            self.require(kwargs, "title")
            data = self.clean_data({key: value for key, value in kwargs.items() if key != "status"})
            data["title"] = self.text(kwargs.get("title"))
            if kwargs.get("status"):
                data["status"] = self.text(kwargs.get("status"))
            record = self.store.create(self.domain, data)
            plan = self.rollback.record(
                "record_remove", self.name, record["id"], {"domain": self.domain, "id": record["id"]}
            )
            return {"record": record, "rollback_plan_id": plan, "rollback": "undo removes this local record"}

        if action == "get":
            self.require(kwargs, "id")
            return {"record": self.store.get(self.domain, self.text(kwargs.get("id")))}

        if action in ("list", "search"):
            if action == "search":
                self.require(kwargs, "query")
            return {
                "records": self.store.list(
                    self.domain,
                    status=kwargs.get("status"),
                    query=self.text(kwargs.get("query")),
                    limit=int(kwargs.get("limit") or 50),
                    start=kwargs.get("start"),
                    end=kwargs.get("end"),
                ),
                "domain": self.domain,
                "local_first": True,
            }

        self.require(kwargs, "id")
        record_id = self.text(kwargs.get("id"))
        if action == "update":
            changes = kwargs.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise CoreToolError("changes must be a non-empty object")
            changes = self.clean_data(changes)
            result = self.store.update(self.domain, record_id, changes)
            plan = self.rollback.record(
                "record_restore",
                self.name,
                record_id,
                {"domain": self.domain, "record": result["before"]},
            )
            return {"record": result["after"], "rollback_plan_id": plan, "rollback": "undo restores the previous record"}

        if action == "delete":
            result = self.store.delete(self.domain, record_id)
            plan = self.rollback.record(
                "record_restore",
                self.name,
                record_id,
                {"domain": self.domain, "record": result["before"]},
            )
            return {"deleted": record_id, "rollback_plan_id": plan, "rollback": "undo restores the soft-deleted record"}

        if action == "complete":
            result = self.store.update(
                self.domain,
                record_id,
                {"status": "done", "completed_at": _utc_now()},
            )
            plan = self.rollback.record(
                "record_restore", self.name, record_id, {"domain": self.domain, "record": result["before"]}
            )
            return {"record": result["after"], "rollback_plan_id": plan, "rollback": "undo restores the previous status"}

        raise CoreToolError(f"Unknown local record action {action!r}")
