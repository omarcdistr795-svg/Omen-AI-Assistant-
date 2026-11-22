"""Memory data models and types for Phase 2.

All items are dated, searchable, editable, and attributable.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryKind(str, Enum):
    PROFILE = "profile"
    PROJECT = "project"
    PREFERENCE = "preference"
    ANALYSIS = "analysis"
    CONVERSATION_SUMMARY = "conversation_summary"
    ENTITY = "entity"
    NOTE = "note"


def now_utc_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def generate_safe_id(prefix: str = "mem") -> str:
    """Generate an ID that will never trigger phone number or secret redaction.

    Redaction pattern for phone numbers matches 8+ consecutive digits or digits
    with dashes/spaces. We interleave letters and digits with at most 1 digit
    per chunk, guaranteeing the phone pattern never matches.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    digits = "0123456789"
    parts = []
    for _ in range(6):
        parts.append(secrets.choice(alphabet))
        parts.append(secrets.choice(alphabet))
        parts.append(secrets.choice(digits))
        parts.append(secrets.choice(alphabet))
    return f"{prefix}_{''.join(parts)}"


@dataclass
class MemoryItem:
    id: str
    text: str
    kind: str = MemoryKind.NOTE.value
    author: str = "soham.bhagat"
    created_at: str = field(default_factory=now_utc_iso)
    updated_at: str = field(default_factory=now_utc_iso)
    expires_at: str | None = None
    is_transient: bool = False
    importance: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    deleted_at: str | None = None

    def is_expired(self, current_time: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        now = current_time or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return now >= exp
        except ValueError:
            return False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryItem:
        return cls(
            id=data["id"],
            text=str(data.get("text", "")),
            kind=str(data.get("kind", MemoryKind.NOTE.value)),
            author=str(data.get("author", "soham.bhagat")),
            created_at=str(data.get("created_at", now_utc_iso())),
            updated_at=str(data.get("updated_at", now_utc_iso())),
            expires_at=data.get("expires_at"),
            is_transient=bool(data.get("is_transient", False)),
            importance=float(data.get("importance", 1.0)),
            metadata=dict(data.get("metadata") or {}),
            version=int(data.get("version", 1)),
            deleted_at=data.get("deleted_at"),
        )


@dataclass
class EntityRecord:
    id: str
    memory_id: str
    entity_type: str  # person, company, tool, repository, document, topic
    name: str
    relation: str = "mentioned"
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_utc_iso)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    item: MemoryItem
    score: float
    match_source: str  # "vector", "fts", or "hybrid"

    def as_dict(self) -> dict[str, Any]:
        res = self.item.as_dict()
        res["search_score"] = round(self.score, 4)
        res["match_source"] = self.match_source
        return res
