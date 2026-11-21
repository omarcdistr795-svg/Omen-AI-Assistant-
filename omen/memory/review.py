"""Terminal view renderer for memory review."""

from __future__ import annotations

from typing import Any


def render_memory_review(data: dict[str, Any]) -> str:
    """Format memory review data into an informative terminal dashboard."""
    status = data.get("status", {})
    profile = data.get("profile", {})
    projects = data.get("projects", [])
    preferences = data.get("preferences", {})
    entities = data.get("entities", [])
    transient = data.get("transient_facts", [])

    lines = [
        "============================= MEMORY REVIEW =============================",
        f"  Total records:    {status.get('records', 0)} ({status.get('vectors_indexed', 0)} vectors indexed)",
        f"  Storage size:     {status.get('storage_bytes', 0) / 1024:.1f} KB",
        f"  Embedding model:  {status.get('embedding_model', 'local_hash')}",
        f"  Background worker: {'PAUSED' if status.get('indexer', {}).get('paused') else 'ACTIVE'}"
        f" (pending: {status.get('indexer', {}).get('pending_tasks', 0)})",
        "",
        "--- Profile ---",
    ]

    if profile:
        for k, v in profile.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (No profile set)")

    lines.append("")
    lines.append("--- Standing Preferences ---")
    if preferences:
        for k, v in preferences.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (No standing preferences set)")

    lines.append("")
    lines.append("--- Active Projects ---")
    if projects:
        for proj in projects:
            lines.append(f"  • {proj.get('name', 'Unnamed')}: {proj.get('goal', 'No goal specified')}")
            lines.append(f"    Progress: {proj.get('progress', 'started')} | Deadline: {proj.get('deadline', 'none')}")
    else:
        lines.append("  (No active projects)")

    lines.append("")
    lines.append("--- Tracked Entities ---")
    if entities:
        for ent in entities:
            lines.append(f"  • {ent.get('name')} [{ent.get('entity_type')}]")
    else:
        lines.append("  (No entities recorded)")

    lines.append("")
    lines.append("--- Transient / Expiring Facts ---")
    if transient:
        for item in transient:
            exp = item.get("expires_at") or "no expiry"
            lines.append(f"  • [{item.get('id')}] {item.get('text')} (expires: {exp})")
    else:
        lines.append("  (No active transient facts)")

    lines.append("=========================================================================")
    return "\n".join(lines)
