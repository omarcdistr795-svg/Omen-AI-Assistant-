"""Harmless demo tools. No network, shell, file writes, or real side effects."""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone

from ..safety.redaction import redact_text

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    raise ValueError("Only arithmetic on numeric literals is allowed")


class DemoEcho:
    name = "demo.echo"
    description = "Echo short text back to the terminal. No side effects."
    permission_class = "read_only"

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 500}},
            "required": ["text"],
            "additionalProperties": False,
        }

    def dry_run(self, **kwargs) -> dict:
        return {"would_echo": redact_text(str(kwargs.get("text", "")))}

    def execute(self, **kwargs) -> dict:
        text = str(kwargs.get("text", ""))
        if not text:
            raise ValueError("text is required")
        return {"echo": redact_text(text), "characters": len(text)}


class DemoCalculate:
    name = "demo.calculate"
    description = "Evaluate a numeric arithmetic expression without eval()."
    permission_class = "read_only"

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"expression": {"type": "string", "maxLength": 200}},
            "required": ["expression"],
            "additionalProperties": False,
        }

    def dry_run(self, **kwargs) -> dict:
        return {"would_evaluate": str(kwargs.get("expression", ""))}

    def execute(self, **kwargs) -> dict:
        expression = str(kwargs.get("expression", ""))
        try:
            tree = ast.parse(expression, mode="eval")
            value = _safe_eval(tree)
        except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
            raise ValueError(f"Invalid arithmetic expression: {exc}") from exc
        return {"expression": expression, "result": value}


class InMemoryNoteStore:
    """Phase 1 stub. Phase 3 owns real notes/files."""

    def __init__(self) -> None:
        self.notes: dict[str, dict] = {}

    def create(self, title: str, body: str) -> dict:
        note_id = f"note-{len(self.notes) + 1}"
        note = {
            "id": note_id,
            "title": redact_text(title),
            "body": redact_text(body),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.notes[note_id] = note
        return note


class DemoNoteCreate:
    name = "demo.note_create"
    description = "Create an in-memory draft note (harmless, no disk writes)."
    permission_class = "low_risk_write"

    def __init__(self, store: InMemoryNoteStore | None = None) -> None:
        self.store = store or InMemoryNoteStore()

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 2000},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        }

    def dry_run(self, **kwargs) -> dict:
        return {
            "would_create_note": {
                "title": redact_text(str(kwargs.get("title", ""))),
                "body": redact_text(str(kwargs.get("body", ""))),
                "storage": "in-memory only",
            }
        }

    def execute(self, **kwargs) -> dict:
        title = str(kwargs.get("title", ""))
        body = str(kwargs.get("body", ""))
        if not title or not body:
            raise ValueError("title and body are required")
        return {"note": self.store.create(title, body), "rollback": "delete in-memory note"}
