"""Omen Memory Store: structured SQLite + vector search + file store.

Persistent, searchable, editable, attributable memory that stays strictly within
resource budgets, rejects secrets, de-emphasizes transient facts, and supports
background re-indexing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .background import BackgroundIndexer
from .filestore import FileStore
from .models import EntityRecord, MemoryItem, MemoryKind, SearchResult, generate_safe_id, now_utc_iso
from .retrieval import compute_recency_multiplier, rank_and_fuse_results
from .secret_rejection import SecretRejectedError, validate_no_secrets
from .vector import (
    LocalEmbeddingModel,
    OllamaEmbeddingModel,
    blob_to_vector,
    cosine_similarity,
    create_embedding_model,
    vector_to_blob,
)

_TRANSIENT_PATTERNS = re.compile(
    r"\b(today|currently|right now|temporarily|weather|traffic|lunch|dinner|flight|temp)\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


class OmenMemoryStore:
    """Production memory store for Phase 2."""

    phase = 2
    implementation = "sqlite-vector-filestore"

    def __init__(
        self,
        config: Any = None,
        *,
        root: Path | str | None = None,
        db_path: Path | str | None = None,
        monitor: Any = None,
        audit: Any = None,
        embedding_model: Any = None,
    ) -> None:
        self.config = config
        self.monitor = monitor
        self.audit = audit

        if db_path is not None:
            self.db_path = Path(db_path)
            self.root = self.db_path.parent
        elif root is not None:
            self.root = Path(root).resolve()
            self.db_path = self.root / "omen_memory.db"
        elif config is not None:
            configured_path = config.get("memory.db_path", "state/memory/omen_memory.db")
            self.db_path = Path(configured_path).resolve()
            self.root = self.db_path.parent
        else:
            self.root = Path("state/memory").resolve()
            self.db_path = self.root / "omen_memory.db"

        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.filestore = FileStore(self.root)
        self.embedding_model = embedding_model or create_embedding_model(self.config)

        self._lock = threading.RLock()
        self._init_db()

        # Background indexer for vector re-embedding
        self.indexer = BackgroundIndexer(
            index_fn=self._index_vector_record,
            monitor=self.monitor,
            on_event=self._on_indexer_event,
        )
        self.indexer.start()

    @contextlib.contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    is_transient INTEGER NOT NULL DEFAULT 0,
                    importance REAL NOT NULL DEFAULT 1.0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
                CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
                CREATE INDEX IF NOT EXISTS idx_memories_author ON memories(author);

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    id UNINDEXED,
                    kind,
                    text,
                    metadata_text,
                    tokenize = 'porter unicode61'
                );

                CREATE TABLE IF NOT EXISTS memory_vectors (
                    memory_id TEXT PRIMARY KEY,
                    embedding_dim INTEGER NOT NULL,
                    embedding_blob BLOB NOT NULL,
                    model_name TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS memory_entities (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'mentioned',
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_entities_name ON memory_entities(name);
                CREATE INDEX IF NOT EXISTS idx_entities_type ON memory_entities(entity_type);
                """
            )
            # SQLite's built-in schema version marker (additive, safe to re-apply).
            conn.execute("PRAGMA user_version = 1;")

    def _on_indexer_event(self, event: dict[str, Any]) -> None:
        if self.audit:
            self.audit.append(
                {
                    "event_type": event.get("event_type", "memory_event"),
                    "tool": "memory_indexer",
                    "status": "ok",
                    "details": event,
                    "summary": f"Memory indexer event: {event.get('reason', '')}",
                }
            )

    def _index_vector_record(self, record_id: str, text: str) -> None:
        """Internal vector indexing callback used by background indexer."""
        vec = self.embedding_model.embed(text)
        blob = vector_to_blob(vec)
        dim = len(vec)
        model_name = getattr(self.embedding_model, "name", "local_hash")
        now = now_utc_iso()

        with self._lock:
            try:
                with self._get_connection() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM memories WHERE id = ?", (record_id,)
                    ).fetchone()
                    if not exists:
                        return
                    conn.execute(
                        """
                        INSERT INTO memory_vectors (memory_id, embedding_dim, embedding_blob, model_name, indexed_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(memory_id) DO UPDATE SET
                            embedding_dim=excluded.embedding_dim,
                            embedding_blob=excluded.embedding_blob,
                            model_name=excluded.model_name,
                            indexed_at=excluded.indexed_at
                        """,
                        (record_id, dim, blob, model_name, now),
                    )
            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                logger.warning("Could not persist vector for %s: %s", record_id, exc)

    def remember(
        self,
        text: str,
        *,
        kind: str = MemoryKind.NOTE.value,
        metadata: dict[str, Any] | None = None,
        author: str = "soham.bhagat",
        expires_at: str | None = None,
        is_transient: bool | None = None,
        importance: float = 1.0,
        wait_for_index: bool = False,
    ) -> dict[str, Any]:
        """Store a new memory item. Enforces secret rejection, assigns safe ID, and indexes."""
        # 1. Secret Rejection
        validate_no_secrets(text, metadata)

        # 2. Auto-detect transient if not explicitly given
        if is_transient is None:
            is_transient = bool(_TRANSIENT_PATTERNS.search(text))

        record_id = generate_safe_id()
        now = now_utc_iso()
        meta = dict(metadata or {})

        item = MemoryItem(
            id=record_id,
            text=str(text).strip(),
            kind=kind,
            author=author,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            is_transient=is_transient,
            importance=max(0.1, min(2.0, float(importance))),
            metadata=meta,
            version=1,
        )

        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, kind, text, author, created_at, updated_at, expires_at,
                    is_transient, importance, metadata_json, version, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    item.id,
                    item.kind,
                    item.text,
                    item.author,
                    item.created_at,
                    item.updated_at,
                    item.expires_at,
                    1 if item.is_transient else 0,
                    item.importance,
                    json.dumps(item.metadata),
                    item.version,
                ),
            )

            # Update FTS
            conn.execute(
                """
                INSERT INTO memory_fts (id, kind, text, metadata_text)
                VALUES (?, ?, ?, ?)
                """,
                (item.id, item.kind, item.text, json.dumps(item.metadata)),
            )

            # Check if this item defines an entity
            if item.kind == MemoryKind.ENTITY.value and "name" in meta:
                conn.execute(
                    """
                    INSERT INTO memory_entities (id, memory_id, entity_type, name, relation, attributes_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generate_safe_id("ent"),
                        item.id,
                        str(meta.get("entity_type", "topic")),
                        str(meta.get("name")),
                        str(meta.get("relation", "defined")),
                        json.dumps(meta.get("attributes", {})),
                        item.created_at,
                    ),
                )

        # 3. Vector indexing (immediate or background)
        if wait_for_index:
            self._index_vector_record(item.id, item.text)
        else:
            self.indexer.enqueue(item.id, item.text)

        return item.as_dict()

    def update(
        self,
        record_id: str,
        *,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
        kind: str | None = None,
        expires_at: str | None = None,
        is_transient: bool | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing memory item. Validates secrets, increments version, updates indexes."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL", (record_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Memory item not found: {record_id}")

            current_text = str(row["text"])
            current_metadata = json.loads(row["metadata_json"])
            new_text = str(text) if text is not None else current_text
            new_metadata = dict(metadata) if metadata is not None else current_metadata

            # Secret rejection
            validate_no_secrets(new_text, new_metadata)

            now = now_utc_iso()
            new_kind = str(kind) if kind is not None else str(row["kind"])
            new_expires = expires_at if expires_at is not None else row["expires_at"]
            new_transient = (
                (1 if is_transient else 0)
                if is_transient is not None
                else int(row["is_transient"])
            )
            new_author = author if author is not None else str(row["author"])
            new_version = int(row["version"]) + 1

            conn.execute(
                """
                UPDATE memories SET
                    text = ?,
                    metadata_json = ?,
                    kind = ?,
                    expires_at = ?,
                    is_transient = ?,
                    author = ?,
                    updated_at = ?,
                    version = ?
                WHERE id = ?
                """,
                (
                    new_text,
                    json.dumps(new_metadata),
                    new_kind,
                    new_expires,
                    new_transient,
                    new_author,
                    now,
                    new_version,
                    record_id,
                ),
            )

            # Update FTS
            conn.execute("DELETE FROM memory_fts WHERE id = ?", (record_id,))
            conn.execute(
                "INSERT INTO memory_fts (id, kind, text, metadata_text) VALUES (?, ?, ?, ?)",
                (record_id, new_kind, new_text, json.dumps(new_metadata)),
            )

        # Trigger re-embedding
        self.indexer.enqueue(record_id, new_text)

        item = self.get(record_id)
        if not item:
            raise RuntimeError(f"Failed to fetch updated item: {record_id}")
        return item

    def forget(self, record_id: str) -> bool:
        """Forget (delete) a memory item by ID from SQLite, FTS, and vector index."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (record_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                conn.execute("DELETE FROM memory_fts WHERE id = ?", (record_id,))
                conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (record_id,))
                conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (record_id,))
        return deleted

    def get(self, record_id: str) -> dict[str, Any] | None:
        """Retrieve memory item by ID."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL", (record_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "text": row["text"],
            "author": row["author"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "is_transient": bool(row["is_transient"]),
            "importance": float(row["importance"]),
            "metadata": json.loads(row["metadata_json"]),
            "version": int(row["version"]),
            "deleted_at": row["deleted_at"],
        }

    def recall(
        self,
        query: str = "",
        *,
        limit: int = 5,
        kind: str | None = None,
        include_expired: bool = False,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining keyword FTS, vector similarity, and recency decay."""
        now = datetime.now(timezone.utc)
        clean_query = query.strip()
        threshold = (
            min_score
            if min_score is not None
            else float(self.config.get("memory.min_similarity_score", 0.1) if self.config else 0.1)
        )

        # If query is empty, return latest memories
        if not clean_query:
            with self._lock, self._get_connection() as conn:
                params: list[Any] = []
                sql = "SELECT * FROM memories WHERE deleted_at IS NULL"
                if kind:
                    sql += " AND kind = ?"
                    params.append(kind)
                sql += " ORDER BY created_at DESC"
                rows = conn.execute(sql, params).fetchall()
                results = []
                for row in rows:
                    item_dict = self._row_to_dict(row)
                    item = MemoryItem.from_dict(item_dict)
                    if not include_expired and item.is_expired(now):
                        continue
                    results.append(item_dict)
                    if len(results) >= limit:
                        break
                return results

        # 1. Full Text Search
        fts_candidates: list[tuple[MemoryItem, float]] = []
        with self._lock, self._get_connection() as conn:
            # Sanitize query for FTS5 (remove special characters)
            safe_query = re.sub(r"[^\w\s]", " ", clean_query).strip()
            if safe_query:
                tokens = safe_query.split()
                # Create OR match for fuzzy keyword match
                match_clause = " OR ".join(f'"{t}"*' for t in tokens)
                try:
                    sql = """
                        SELECT m.*, bm25(memory_fts) as rank
                        FROM memory_fts
                        JOIN memories m ON m.id = memory_fts.id
                        WHERE memory_fts MATCH ? AND m.deleted_at IS NULL
                    """
                    params = [match_clause]
                    if kind:
                        sql += " AND m.kind = ?"
                        params.append(kind)
                    sql += " ORDER BY rank LIMIT ?"
                    params.append(limit * 3)

                    rows = conn.execute(sql, params).fetchall()
                    for row in rows:
                        item = MemoryItem.from_dict(self._row_to_dict(row))
                        # BM25 is negative in SQLite; convert to positive 0..1 scale
                        raw_rank = float(row["rank"])
                        fts_score = min(1.0, max(0.4, -raw_rank))
                        fts_candidates.append((item, fts_score))
                except sqlite3.OperationalError as exc:
                    # Never log the raw query: it may contain user-supplied secrets.
                    logger.warning("Full-text memory query failed: %s", exc)

        # 2. Vector Search
        query_vec = self.embedding_model.embed(clean_query)
        vector_candidates: list[tuple[MemoryItem, float]] = []

        with self._lock, self._get_connection() as conn:
            sql = """
                SELECT m.*, v.embedding_blob
                FROM memories m
                JOIN memory_vectors v ON m.id = v.memory_id
                WHERE m.deleted_at IS NULL
            """
            params = []
            if kind:
                sql += " AND m.kind = ?"
                params.append(kind)

            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                item = MemoryItem.from_dict(self._row_to_dict(row))
                item_vec = blob_to_vector(row["embedding_blob"])
                sim = cosine_similarity(query_vec, item_vec)
                vector_candidates.append((item, sim))

        # 3. Fuse and rank
        fused = rank_and_fuse_results(
            fts_candidates,
            vector_candidates,
            limit=limit,
            min_score=threshold,
            include_expired=include_expired,
            current_time=now,
        )

        return [res.as_dict() for res in fused]

    # --- Domain Memory Specializations ---

    # 1. Profile Memory
    def set_profile(self, key: str, value: Any, author: str = "soham.bhagat") -> dict[str, Any]:
        """Store or update a profile attribute."""
        text = f"{key}: {value}"
        existing = self.recall(query=key, limit=5, kind=MemoryKind.PROFILE.value)
        match = next((item for item in existing if item.get("metadata", {}).get("profile_key") == key), None)
        if match:
            return self.update(match["id"], text=text, metadata={"profile_key": key, "value": value}, author=author)
        return self.remember(
            text=text,
            kind=MemoryKind.PROFILE.value,
            metadata={"profile_key": key, "value": value},
            author=author,
            wait_for_index=True,
        )

    def get_profile(self) -> dict[str, Any]:
        """Retrieve full aggregated personal profile."""
        items = self.recall(query="", limit=50, kind=MemoryKind.PROFILE.value)
        profile: dict[str, Any] = {}
        for item in items:
            key = item.get("metadata", {}).get("profile_key")
            val = item.get("metadata", {}).get("value")
            if key:
                profile[key] = val
            else:
                profile[item["id"]] = item["text"]
        return profile

    # 2. Projects Memory
    def store_project(
        self,
        name: str,
        goal: str,
        *,
        repository: str = "",
        stakeholders: list[str] | None = None,
        deadline: str = "",
        decisions: list[str] | None = None,
        open_questions: list[str] | None = None,
        blockers: list[str] | None = None,
        progress: str = "started",
        author: str = "soham.bhagat",
    ) -> dict[str, Any]:
        """Store or update a project record."""
        meta = {
            "name": name,
            "goal": goal,
            "repository": repository,
            "stakeholders": stakeholders or [],
            "deadline": deadline,
            "decisions": decisions or [],
            "open_questions": open_questions or [],
            "blockers": blockers or [],
            "progress": progress,
        }
        text = f"Project {name}: {goal}. Progress: {progress}. Deadline: {deadline}."
        existing = self.recall(query=name, limit=5, kind=MemoryKind.PROJECT.value)
        match = next((item for item in existing if item.get("metadata", {}).get("name", "").lower() == name.lower()), None)
        if match:
            return self.update(match["id"], text=text, metadata=meta, author=author)
        return self.remember(
            text=text,
            kind=MemoryKind.PROJECT.value,
            metadata=meta,
            author=author,
            wait_for_index=True,
        )

    def get_project(self, name: str) -> dict[str, Any] | None:
        """Find a project by name."""
        items = self.recall(query=name, limit=5, kind=MemoryKind.PROJECT.value)
        for item in items:
            if item.get("metadata", {}).get("name", "").lower() == name.lower():
                return item
        return None

    def list_projects(self) -> list[dict[str, Any]]:
        """List all tracked projects."""
        return self.recall(query="", limit=50, kind=MemoryKind.PROJECT.value)

    # 3. Preferences Memory
    def set_preference(self, key: str, value: Any, author: str = "soham.bhagat") -> dict[str, Any]:
        """Store or update a standing preference."""
        text = f"Preference {key}: {value}"
        existing = self.recall(query=key, limit=5, kind=MemoryKind.PREFERENCE.value)
        match = next((item for item in existing if item.get("metadata", {}).get("pref_key") == key), None)
        if match:
            return self.update(match["id"], text=text, metadata={"pref_key": key, "value": value}, author=author)
        return self.remember(
            text=text,
            kind=MemoryKind.PREFERENCE.value,
            metadata={"pref_key": key, "value": value},
            author=author,
            wait_for_index=True,
        )

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get preference value by key."""
        prefs = self.list_preferences()
        return prefs.get(key, default)

    def list_preferences(self) -> dict[str, Any]:
        """Return dict of all user preferences."""
        items = self.recall(query="", limit=50, kind=MemoryKind.PREFERENCE.value)
        result = {}
        for item in items:
            key = item.get("metadata", {}).get("pref_key")
            val = item.get("metadata", {}).get("value")
            if key:
                result[key] = val
        return result

    # 4. Past Analyses
    def store_analysis(
        self,
        topic: str,
        findings: str,
        *,
        analysis_type: str = "research",
        lessons: str = "",
        decisions: list[str] | None = None,
        source_links: list[str] | None = None,
        author: str = "soham.bhagat",
    ) -> dict[str, Any]:
        """Store past research, code decisions, incident notes, or lessons."""
        text = f"Analysis on {topic}: {findings}. Lessons: {lessons}."
        meta = {
            "topic": topic,
            "analysis_type": analysis_type,
            "findings": findings,
            "lessons": lessons,
            "decisions": decisions or [],
            "source_links": source_links or [],
        }
        return self.remember(
            text=text,
            kind=MemoryKind.ANALYSIS.value,
            metadata=meta,
            author=author,
            wait_for_index=True,
        )

    def list_analyses(self, limit: int = 50) -> list[dict[str, Any]]:
        """List all past analyses."""
        return self.recall(query="", limit=limit, kind=MemoryKind.ANALYSIS.value)

    # 5. Conversation Summaries
    def store_conversation_summary(
        self,
        summary: str,
        *,
        session_id: str = "default",
        turn_range: tuple[int, int] = (0, 0),
        source_messages: list[str] | None = None,
        linked_files: list[str] | None = None,
        linked_tools: list[str] | None = None,
        author: str = "omen",
    ) -> dict[str, Any]:
        """Store concise summary of conversation rather than unbounded raw transcripts."""
        meta = {
            "session_id": session_id,
            "turn_range": list(turn_range),
            "source_messages": source_messages or [],
            "linked_files": linked_files or [],
            "linked_tools": linked_tools or [],
        }
        return self.remember(
            text=summary,
            kind=MemoryKind.CONVERSATION_SUMMARY.value,
            metadata=meta,
            author=author,
            wait_for_index=True,
        )

    def list_conversation_summaries(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List conversation summaries, optionally filtered by session."""
        items = self.recall(query="", limit=limit, kind=MemoryKind.CONVERSATION_SUMMARY.value)
        if session_id:
            return [it for it in items if it.get("metadata", {}).get("session_id") == session_id]
        return items

    # 6. Entity Memory
    def remember_entity(
        self,
        name: str,
        entity_type: str,
        *,
        details: str = "",
        relations: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
        author: str = "soham.bhagat",
    ) -> dict[str, Any]:
        """Store entity memory: person, company, tool, repository, document, or topic."""
        meta = {
            "name": name,
            "entity_type": entity_type,
            "details": details,
            "relations": relations or [],
            "attributes": attributes or {},
        }
        text = f"Entity {name} ({entity_type}): {details}"
        existing = self.recall(query=name, limit=5, kind=MemoryKind.ENTITY.value)
        match = next((item for item in existing if item.get("metadata", {}).get("name", "").lower() == name.lower()), None)
        if match:
            return self.update(match["id"], text=text, metadata=meta, author=author)
        return self.remember(
            text=text,
            kind=MemoryKind.ENTITY.value,
            metadata=meta,
            author=author,
            wait_for_index=True,
        )

    def find_entities(self, query: str) -> list[dict[str, Any]]:
        """Find entity records by name or query."""
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_entities
                WHERE name LIKE ? OR entity_type LIKE ?
                ORDER BY created_at DESC LIMIT 20
                """,
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "memory_id": row["memory_id"],
                    "name": row["name"],
                    "entity_type": row["entity_type"],
                    "relation": row["relation"],
                    "attributes": json.loads(row["attributes_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    # --- Maintenance & Lifecycle ---

    def purge_expired(self) -> int:
        """Purge all expired items from database."""
        now = now_utc_iso()
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()
            expired_ids = [row["id"] for row in rows]
            for rec_id in expired_ids:
                conn.execute("DELETE FROM memories WHERE id = ?", (rec_id,))
                conn.execute("DELETE FROM memory_fts WHERE id = ?", (rec_id,))
                conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (rec_id,))
                conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (rec_id,))
            return len(expired_ids)

    def export_data(self, path: Path | str | None = None) -> dict[str, Any]:
        """Export all memories, entities, and metadata to JSON format."""
        with self._lock, self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM memories WHERE deleted_at IS NULL").fetchall()
            memories = [self._row_to_dict(r) for r in rows]
            entity_rows = conn.execute("SELECT * FROM memory_entities").fetchall()
            entities = [
                {
                    "id": r["id"],
                    "memory_id": r["memory_id"],
                    "name": r["name"],
                    "entity_type": r["entity_type"],
                    "relation": r["relation"],
                    "attributes": json.loads(r["attributes_json"]),
                    "created_at": r["created_at"],
                }
                for r in entity_rows
            ]

        export_payload = {
            "version": "1.0",
            "exported_at": now_utc_iso(),
            "counts": {"memories": len(memories), "entities": len(entities)},
            "memories": memories,
            "entities": entities,
        }

        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(export_payload, indent=2), encoding="utf-8")

        return export_payload

    def restore_data(
        self, data_or_path: dict[str, Any] | str | Path, *, overwrite: bool = False
    ) -> dict[str, Any]:
        """Restore memory items from exported data. Enforces secret rejection."""
        if isinstance(data_or_path, (str, Path)):
            raw_text = Path(data_or_path).read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        else:
            payload = data_or_path

        memories = payload.get("memories") or []

        # Validate no secrets in entire import payload before writing
        for mem in memories:
            validate_no_secrets(mem.get("text", ""), mem.get("metadata"))

        restored_count = 0
        restored_entities = 0
        entities = payload.get("entities") or []
        with self._lock:
            if overwrite:
                with self._get_connection() as conn:
                    conn.execute("DELETE FROM memories;")
                    conn.execute("DELETE FROM memory_fts;")
                    conn.execute("DELETE FROM memory_vectors;")
                    conn.execute("DELETE FROM memory_entities;")

            with self._get_connection() as conn:
                for mem in memories:
                    rec_id = mem.get("id") or generate_safe_id()
                    kind = str(mem.get("kind", MemoryKind.NOTE.value))
                    text = str(mem.get("text", "")).strip()
                    author = str(mem.get("author", "soham.bhagat"))
                    created_at = str(mem.get("created_at", now_utc_iso()))
                    updated_at = str(mem.get("updated_at", now_utc_iso()))
                    expires_at = mem.get("expires_at")
                    is_transient = 1 if mem.get("is_transient") else 0
                    importance = float(mem.get("importance", 1.0))
                    metadata = mem.get("metadata") or {}

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memories (
                            id, kind, text, author, created_at, updated_at, expires_at,
                            is_transient, importance, metadata_json, version, deleted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            rec_id,
                            kind,
                            text,
                            author,
                            created_at,
                            updated_at,
                            expires_at,
                            is_transient,
                            importance,
                            json.dumps(metadata),
                            mem.get("version", 1),
                        ),
                    )
                    conn.execute("DELETE FROM memory_fts WHERE id = ?", (rec_id,))
                    conn.execute(
                        "INSERT INTO memory_fts (id, kind, text, metadata_text) VALUES (?, ?, ?, ?)",
                        (rec_id, kind, text, json.dumps(metadata)),
                    )
                    restored_count += 1

                # Entities are part of the export and must survive a restore.
                for ent in entities:
                    entity_name = str(ent.get("name", ""))
                    relation = str(ent.get("relation", "mentioned"))
                    attributes = dict(ent.get("attributes") or {})
                    validate_no_secrets(entity_name, attributes)
                    validate_no_secrets(relation)
                    memory_id = str(ent.get("memory_id", ""))
                    if not memory_id:
                        raise ValueError(f"Entity {entity_name!r} has no memory_id")
                    if not conn.execute(
                        "SELECT 1 FROM memories WHERE id = ?", (memory_id,)
                    ).fetchone():
                        raise ValueError(
                            f"Entity {entity_name!r} references unknown memory id {memory_id!r}"
                        )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memory_entities (
                            id, memory_id, entity_type, name, relation, attributes_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(ent.get("id") or generate_safe_id("ent")),
                            memory_id,
                            str(ent.get("entity_type", "topic")),
                            entity_name,
                            relation,
                            json.dumps(attributes),
                            str(ent.get("created_at") or now_utc_iso()),
                        ),
                    )
                    restored_entities += 1

            for mem in memories:
                rec_id = mem.get("id")
                text = mem.get("text", "")
                if rec_id and text:
                    self._index_vector_record(rec_id, text)

        return {"restored_memories": restored_count, "restored_entities": restored_entities}

    def backup(self, backup_dir: Path | str | None = None) -> Path:
        """Create a compressed snapshot of database and files."""
        if backup_dir:
            self.filestore.backups_dir = Path(backup_dir)
            self.filestore.backups_dir.mkdir(parents=True, exist_ok=True)
        return self.filestore.create_backup_archive(self.db_path)

    # --- Background Indexer Control ---

    def pause_indexing(self) -> None:
        self.indexer.pause()

    def resume_indexing(self) -> None:
        self.indexer.resume()

    def is_indexing_paused(self) -> bool:
        return self.indexer.is_paused()

    def flush_indexer(self, timeout: float = 5.0) -> bool:
        return self.indexer.flush(timeout=timeout)

    def close(self) -> None:
        self.indexer.stop()

    # --- Status and Review ---

    def status(self) -> dict[str, Any]:
        """Return memory subsystem health and metrics."""
        with self._lock, self._get_connection() as conn:
            total_records = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()[0]
            vector_count = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
            entity_count = conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]

            rows = conn.execute(
                "SELECT kind, COUNT(*) as c FROM memories WHERE deleted_at IS NULL GROUP BY kind"
            ).fetchall()
            kind_counts = {row["kind"]: row["c"] for row in rows}

        db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "phase": self.phase,
            "implementation": self.implementation,
            "records": total_records,
            "vectors_indexed": vector_count,
            "entities": entity_count,
            "by_kind": kind_counts,
            "storage_bytes": db_size_bytes,
            "indexer": self.indexer.status(),
            "embedding_model": getattr(self.embedding_model, "name", "local_hash"),
        }

    def get_review_data(self) -> dict[str, Any]:
        """Return structured data for the terminal memory review view."""
        stat = self.status()
        profile = self.get_profile()
        projects = self.list_projects()
        preferences = self.list_preferences()
        entities = self.find_entities("")
        recent_notes = self.recall(query="", limit=5, kind=MemoryKind.NOTE.value)

        # Transient and expiring facts
        now = now_utc_iso()
        with self._lock, self._get_connection() as conn:
            transient_rows = conn.execute(
                "SELECT * FROM memories WHERE is_transient = 1 AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            transient_items = [self._row_to_dict(r) for r in transient_rows]

        return {
            "status": stat,
            "profile": profile,
            "projects": [p.get("metadata", {}) for p in projects[:5]],
            "preferences": preferences,
            "entities": entities[:5],
            "recent_notes": recent_notes,
            "transient_facts": transient_items,
        }
