"""Desktop system control. Every action is allowlisted and approval-gated."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, truncate

_MEDIA_ACTIONS = {"play": "play", "pause": "pause", "next": "next", "previous": "previous", "status": "status"}
_WINDOW_ACTIONS = {"clients": "clients", "workspaces": "getworkspaces", "active": "activewindow"}
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9_.:/@-]{1,120}$")


class SystemController:
    def __init__(self, config: Any) -> None:
        raw_apps = config.get("core_tools.system.open_apps") or {}
        if not isinstance(raw_apps, dict):
            raise CoreToolError("core_tools.system.open_apps must be a mapping")
        self.open_apps: dict[str, list[str]] = {
            str(name): shlex.split(str(argv)) for name, argv in raw_apps.items() if str(argv).strip()
        }
        self.media_command = str(config.get("core_tools.system.media_command") or "")
        self.window_command = str(config.get("core_tools.system.window_command") or "")
        self.timeout = float(config.get("core_tools.system.timeout_seconds", 5))
        self.env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR") if key in os.environ}


    def _which(self, program: str) -> str:
        found = shutil.which(program)
        if found is None:
            raise CoreToolError(f"System command is not installed: {program}")
        return found

    def _run(self, argv: list[str]) -> dict[str, Any]:
        self._which(argv[0])
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout, shell=False, env=self.env, check=False
        )
        return {
            "command": redact_value(argv),
            "exit_code": completed.returncode,
            "stdout": redact_value(truncate(completed.stdout, 8000)),
            "stderr": redact_value(truncate(completed.stderr, 8000)),
            "shell": False,
        }

    def open_app(self, name: str, args: list[str] | None = None) -> dict[str, Any]:
        base = self.open_apps.get(name)
        if base is None:
            raise CoreToolError(f"Application {name!r} is not in the system-control allowlist")
        extra = [str(item) for item in (args or [])]
        for item in extra:
            if not _SAFE_TARGET.fullmatch(item):
                raise CoreToolError(f"Unsafe application argument: {item!r}")
        argv = [*base, *extra]
        self._which(argv[0])
        devnull = os.devnull
        file_actions = (
            (os.POSIX_SPAWN_OPEN, 0, devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_OPEN, 2, devnull, os.O_WRONLY, 0),
        )
        pid = os.posix_spawn(
            self._which(argv[0]), argv, self.env, file_actions=file_actions, setsid=True
        )
        return {"opened": name, "pid": pid, "command": redact_value(argv), "shell": False}

    def media(self, action: str) -> dict[str, Any]:
        if action not in _MEDIA_ACTIONS:
            raise CoreToolError(f"Media action {action!r} is not allowed")
        if not self.media_command:
            raise CoreToolError("No media command is configured (BLOCKED-EXTERNAL)")
        argv = shlex.split(self.media_command)
        return self._run([*argv, _MEDIA_ACTIONS[action]])

    def window(self, action: str, target: str | None = None) -> dict[str, Any]:
        if action not in _WINDOW_ACTIONS:
            raise CoreToolError(f"Window action {action!r} is not allowed")
        if not self.window_command:
            raise CoreToolError("No window command is configured (BLOCKED-EXTERNAL)")
        if target is not None and not _SAFE_TARGET.fullmatch(str(target)):
            raise CoreToolError(f"Unsafe window target: {target!r}")
        argv = shlex.split(self.window_command) + [_WINDOW_ACTIONS[action]]
        if target:
            argv.append(str(target))
        return self._run(argv)

    def status(self) -> dict[str, Any]:
        return {
            "open_apps": sorted(self.open_apps),
            "media": {"configured": bool(self.media_command), "actions": sorted(_MEDIA_ACTIONS)},
            "window": {"configured": bool(self.window_command), "actions": sorted(_WINDOW_ACTIONS)},
            "safety": "all actions pass through policy -> approval -> audit",
        }


class SystemStatus(CoreTool):
    name = "system.status"
    description = "Report which system-control capabilities are configured (read-only)."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, controller: SystemController) -> None:
        self.controller = controller

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self.controller.status()


class SystemOpenApp(CoreTool):
    name = "system.open_app"
    description = "Open an allowlisted desktop application (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "args": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, controller: SystemController) -> None:
        self.controller = controller

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {"tool": self.name, "dry_run": True, "would_open": self.text(kwargs.get("name")), "side_effects": "none"}

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "name")
        return self.controller.open_app(self.text(kwargs.get("name")), kwargs.get("args"))


class SystemMedia(CoreTool):
    name = "system.media"
    description = "Control media playback through an allowlisted local command (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": sorted(_MEDIA_ACTIONS)}},
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, controller: SystemController) -> None:
        self.controller = controller

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "action")
        return self.controller.media(self.text(kwargs.get("action")))


class SystemWindow(CoreTool):
    name = "system.window"
    description = "Inspect or focus windows through an allowlisted local command (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_WINDOW_ACTIONS)},
            "target": {"type": "string", "maxLength": 120},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, controller: SystemController) -> None:
        self.controller = controller

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "action")
        return self.controller.window(self.text(kwargs.get("action")), kwargs.get("target"))
