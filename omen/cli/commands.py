"""Terminal command parsing and dispatch."""

from __future__ import annotations

from typing import Any, Callable

from ..app import OmenApp
from .render import render_approval_card, render_json, render_status

COMMANDS = (
    "/help",
    "/brief",
    "/status",
    "/dashboard",
    "/remember",
    "/update",
    "/forget",
    "/memory",
    "/show-memory",
    "/review-memory",
    "/export-memory",
    "/backup-memory",
    "/approve",
    "/deny",
    "/history",
    "/tools",
    "/health",
    "/plan",
    "/dry-run",
    "/kill",
    "/guardian",
    "/alerts",
    "/remind-repo",
    "/remind-meeting",
    "/bye",
)


def parse_command(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(None, 1)
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


def handle_command(app: OmenApp, line: str, emit: Callable[[str], None]) -> bool:
    """Return True when the line was a command (including unknown ones)."""
    parsed = parse_command(line)
    if parsed is None:
        return False
    name, arg = parsed
    if name == "/bye":
        emit("Good night. Keep your secrets close and your permissions closer.")
        return True
    if name == "/help":
        emit("Commands: " + ", ".join(COMMANDS))
        return True
    if name == "/status":
        emit(render_status(app.status()))
    elif name == "/dashboard":
        sub = arg.strip().lower() or "status"
        try:
            if sub in ("start", "on"):
                emit(render_json("DASHBOARD", app.start_dashboard()))
            elif sub in ("stop", "off"):
                emit(render_json("DASHBOARD", app.stop_dashboard()))
            elif sub == "status":
                emit(render_json("DASHBOARD", app.dashboard.status() if app.dashboard else {"enabled": bool(app.config.get("dashboard.enabled", True)), "running": False}))
            else:
                emit("Usage: /dashboard [start|stop|status]")
        except Exception as exc:
            emit(f"Dashboard error: {exc}")
    elif name == "/health":
        health = app.health()
        emit(render_json("HEALTH", health))
        if health["status"] != "ok":
            app._notice(f"Health check is {health['status']}; inspect /health details")
    elif name == "/tools":
        emit(render_json("TOOLS", app.tools.schemas()))
    elif name in ("/plan", "/dry-run"):
        pieces = arg.split(None, 1)
        if len(pieces) != 2:
            emit(f"Usage: {name} <tool> <json-params>")
            return True
        try:
            params = app._parse_json(pieces[1])
        except ValueError as exc:
            emit(f"Invalid parameters: {exc}")
            return True
        result = app.run_tool(pieces[0], params, dry_run=(name == "/dry-run"))
        if result.approval_card:
            emit(render_approval_card(result.approval_card))
        else:
            emit(render_json("TOOL", result.as_dict()))
    elif name in ("/approve", "/deny"):
        if not arg.strip():
            emit(f"Usage: {name} <token-id>")
            return True
        try:
            emit(render_json("APPROVAL", app.approve(arg.strip()) if name == "/approve" else app.deny(arg.strip())))
        except Exception as exc:  # report the real error; never silently approve
            emit(f"Approval error: {exc}")
    elif name == "/kill":
        emit(render_json("KILL", app.kill(arg or "user requested halt")))
    elif name == "/history":
        filters: dict[str, Any] = {}
        for token in arg.split():
            if "=" not in token:
                emit("History filters must look like key=value (e.g. status=blocked limit=10)")
                return True
            key, value = token.split("=", 1)
            filters[key] = int(value) if key == "limit" else value
        emit(render_json("HISTORY", app.history(**filters)))
    elif name == "/brief":
        kind = arg.strip().lower() or "morning"
        if kind not in ("morning", "evening"):
            emit("Usage: /brief [morning|evening]")
            return True
        emit(render_json(f"BRIEF ({kind.upper()})", app.brief(kind)))
    elif name in ("/guardian", "/alerts", "/remind-repo", "/remind-meeting"):
        if app.automation is None:
            emit("Automation is disabled in configuration.")
            return True
        try:
            if name == "/guardian":
                result = app.automation.guardian.check()
            elif name == "/alerts":
                result = app.automation.alerts.scan()
            elif name == "/remind-repo":
                if not arg.strip():
                    emit("Usage: /remind-repo <message>")
                    return True
                result = app.automation.contextual.add(arg.strip(), repo=str(app.root))
            else:
                if not arg.strip():
                    emit("Usage: /remind-meeting <message>")
                    return True
                result = app.automation.contextual.add(arg.strip(), meeting=arg.strip())
            emit(render_json(name.upper().lstrip("/"), result))
        except Exception as exc:
            emit(f"Automation error: {exc}")
    elif name == "/remember":
        if not arg.strip():
            emit("Usage: /remember <text>")
            return True
        try:
            emit(render_json("REMEMBER", app.memory.remember(arg.strip())))
        except Exception as exc:
            emit(f"Memory error: {exc}")
    elif name == "/update":
        pieces = arg.split(None, 1)
        if len(pieces) < 2:
            emit("Usage: /update <record-id> <text>")
            return True
        try:
            emit(render_json("UPDATE", app.memory.update(pieces[0], text=pieces[1])))
        except Exception as exc:
            emit(f"Update error: {exc}")
    elif name == "/forget":
        if not arg.strip():
            emit("Usage: /forget <record-id>")
            return True
        emit(render_json("FORGET", {"removed": app.memory.forget(arg.strip())}))
    elif name in ("/memory", "/show-memory") or (name == "/show" and arg.strip().lower() == "memory"):
        clean_arg = arg.strip()
        if clean_arg and clean_arg.lower() != "memory":
            emit(render_json("MEMORY", app.memory.recall(clean_arg)))
        else:
            if hasattr(app.memory, "get_review_data"):
                from ..memory.review import render_memory_review

                emit(render_memory_review(app.memory.get_review_data()))
            else:
                emit(render_json("MEMORY", app.memory.recall()))
    elif name == "/review-memory":
        if hasattr(app.memory, "get_review_data"):
            from ..memory.review import render_memory_review

            emit(render_memory_review(app.memory.get_review_data()))
        else:
            emit(render_json("MEMORY_REVIEW", app.memory.status()))
    elif name == "/export-memory":
        target = arg.strip() or None
        try:
            res = app.memory.export_data(target)
            emit(render_json("EXPORT_MEMORY", {"exported": True, "counts": res.get("counts")}))
        except Exception as exc:
            emit(f"Export error: {exc}")
    elif name == "/backup-memory":
        target = arg.strip() or None
        try:
            backup_path = app.memory.backup(target)
            emit(render_json("BACKUP_MEMORY", {"backup_file": str(backup_path)}))
        except Exception as exc:
            emit(f"Backup error: {exc}")
    else:
        emit(f"Unknown command {name}. Try /help.")
    return True
