"""Hybrid memory retrieval with vector similarity, full-text search, and recency decay.

Transient facts are de-emphasized automatically over time; expired facts
are filtered out.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Sequence

from .models import MemoryItem, SearchResult
from .vector import cosine_similarity


def compute_recency_multiplier(
    item: MemoryItem,
    current_time: datetime | None = None,
    transient_half_life_days: float = 2.0,
) -> float:
    """Compute recency weight multiplier.

    Permanent items maintain a multiplier of 1.0.
    Transient items decay exponentially according to their age and half-life.
    """
    if not item.is_transient:
        return 1.0

    now = current_time or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(item.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (now - created).total_seconds())
        age_days = age_seconds / 86400.0
        # Exponential half-life decay: 0.5^(age / half_life)
        return float(math.pow(0.5, age_days / transient_half_life_days))
    except Exception:
        return 0.5


def rank_and_fuse_results(
    fts_items: list[tuple[MemoryItem, float]],
    vector_items: list[tuple[MemoryItem, float]],
    *,
    limit: int = 5,
    min_score: float = 0.0,
    include_expired: bool = False,
    current_time: datetime | None = None,
    transient_half_life_days: float = 2.0,
) -> list[SearchResult]:
    """Fuse keyword FTS results and semantic vector results using Reciprocal Rank Fusion / Weighted Scoring."""
    now = current_time or datetime.now(timezone.utc)
    scores: dict[str, dict[str, Any]] = {}

    for item, raw_score in fts_items:
        if not include_expired and item.is_expired(now):
            continue
        scores[item.id] = {
            "item": item,
            "fts_score": raw_score,
            "vector_score": 0.0,
            "source": "fts",
        }

    for item, raw_score in vector_items:
        if not include_expired and item.is_expired(now):
            continue
        if item.id in scores:
            scores[item.id]["vector_score"] = raw_score
            scores[item.id]["source"] = "hybrid"
        else:
            scores[item.id] = {
                "item": item,
                "fts_score": 0.0,
                "vector_score": raw_score,
                "source": "vector",
            }

    results: list[SearchResult] = []
    for entry in scores.values():
        item: MemoryItem = entry["item"]
        fts_norm = max(0.0, min(1.0, entry["fts_score"]))
        vec_norm = max(0.0, min(1.0, entry["vector_score"]))

        # Hybrid fusion: reward items matching both keyword and semantic intent
        if entry["source"] == "hybrid":
            base_score = 0.5 * vec_norm + 0.5 * fts_norm + 0.2
        elif entry["source"] == "fts":
            base_score = fts_norm
        else:
            base_score = vec_norm

        recency = compute_recency_multiplier(item, now, transient_half_life_days)
        final_score = base_score * recency * item.importance

        if final_score >= min_score:
            results.append(
                SearchResult(item=item, score=final_score, match_source=entry["source"])
            )

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:limit]


def format_memory_context_for_prompt(memories: Sequence[dict[str, Any]]) -> str:
    """Format retrieved memories into a concise context block for the model prompt."""
    if not memories:
        return ""

    lines = ["Relevant Context & Memory:"]
    for mem in memories:
        kind = mem.get("kind", "note")
        text = mem.get("text", "").strip()
        metadata = mem.get("metadata") or {}
        date_str = mem.get("created_at", "")[:10]

        extra_info = []
        if kind == "project" and "name" in metadata:
            extra_info.append(f"project: {metadata['name']}")
        elif kind == "entity" and "name" in metadata:
            extra_info.append(f"entity: {metadata['name']}")

        extra = f" ({', '.join(extra_info)})" if extra_info else ""
        lines.append(f"- [{kind}] {text}{extra} [{date_str}]")

    return "\n".join(lines)
