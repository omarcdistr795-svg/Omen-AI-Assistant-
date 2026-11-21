"""Sandboxed shell execution: no shell interpreter, no network, no secrets."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, truncate

MANDATORY_DENY = frozenset(
    {
        "rm",
        "rmdir",
        "sudo",
        "su",
        "doas",
        "curl",
        "wget",
        "nc",
        "ncat",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "bash",
        "sh",
        "zsh",
        "fish",
        "python",
        "python3",
        "pip",
        "pip3",
        "eval",
        "exec",
        "git",
        "chmod",
        "chown",
    }
)
_BLOCKED_TEXT = (";", "|", "&", ">", "<", "`", "$(", "${", "\n", "\r", "\x00")


class ShellSandbox:
    """Allowlisted argv execution with a scrubbed environment and timeouts."""

    def __init__(self, config: Any, root: Path | str) -> None:
        base = Path(root).resolve()
        self.allow = {str(item) for item in (config.get("core_tools.shell.allow_commands") or ())}
        self.deny = MANDATORY_DENY | {str(item) for item in (config.get("core_tools.shell.deny_commands") or ())}
        if self.allow & self.deny:
            raise CoreToolError(f"Shell allowlist overlaps denylist: {sorted(self.allow & self.deny)}")
        roots = list(config.get("core_tools.shell.allowed_roots") or ["."])
        self.roots: tuple[Path, ...] = tuple(
            (base / str(item)).resolve() if not Path(str(item)).is_absolute() else Path(str(item)).resolve()
            for item in roots
        )
        if not self.roots:
            raise CoreToolError("Shell sandbox needs at least one allowed root")
        self.timeout = float(config.get("core_tools.shell.timeout_seconds", 5))
        self.max_output = int(config.get("core_tools.shell.max_output_chars", 20_000))
        self.env_allowlist = tuple(
            config.get("core_tools.shell.env_allowlist") or ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
        )
        self.exact_commands: tuple[tuple[str, ...], ...] = tuple(
            tuple(shlex.split(str(item))) for item in (config.get("core_tools.tests.commands") or ())
        )

    def _cwd(self, cwd: Any) -> Path:
        raw = str(cwd or self.roots[0])
        path = Path(raw)
        if not path.is_absolute():
            path = self.roots[0] / path
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in self.roots):
            raise CoreToolError(f"Working directory is outside the sandbox: {raw}")
        if not resolved.is_dir():
            raise CoreToolError(f"Working directory does not exist: {raw}")
        return resolved

    def plan(self, command: str, cwd: Any = None) -> dict[str, Any]:
        """Validate a command without executing it."""
        raw = str(command or "")
        if not raw.strip():
            raise CoreToolError("A command is required")
        for fragment in _BLOCKED_TEXT:
            if fragment in raw:
                raise CoreToolError(f"Shell metacharacter or redirection is blocked: {fragment!r}")
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            raise CoreToolError(f"Could not parse command: {exc}") from exc
        if not argv:
            raise CoreToolError("A command is required")
        program = Path(argv[0]).name
        if program not in self.allow:
            raise CoreToolError(f"Command {program!r} is not in the shell allowlist")
        if program in self.deny:
            raise CoreToolError(f"Command {program!r} is explicitly denied")
        if shutil.which(argv[0]) is None:
            raise CoreToolError(f"Command {program!r} is not installed")
        resolved_cwd = self._cwd(cwd)
        return {"argv": argv, "cwd": str(resolved_cwd), "program": program, "shell": False}

    def _execute(self, argv: list[str], cwd: Path) -> dict[str, Any]:
        env = {key: os.environ[key] for key in self.env_allowlist if key in os.environ}
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        return {
            "command": redact_value(list(argv)),
            "exit_code": completed.returncode,
            "stdout": redact_value(truncate(completed.stdout, self.max_output)),
            "stderr": redact_value(truncate(completed.stderr, self.max_output)),
            "cwd": str(cwd),
            "sandbox": {"shell": False, "network_tools_denied": True, "env_scrubbed": True, "timeout_seconds": self.timeout},
        }

    def run(self, command: str, cwd: Any = None) -> dict[str, Any]:
        plan = self.plan(command, cwd)
        return self._execute(plan["argv"], Path(plan["cwd"]))

    def run_exact(self, argv: list[str], cwd: Any = None) -> dict[str, Any]:
        """Run a pre-registered command (used by tests.run), still sandboxed."""
        tuple_argv = tuple(str(item) for item in argv)
        if tuple_argv not in self.exact_commands:
            raise CoreToolError(f"Command is not in the exact test command allowlist: {list(tuple_argv)}")
        return self._execute(list(tuple_argv), self._cwd(cwd))

    def status(self) -> dict[str, Any]:
        return {
            "allowlist": sorted(self.allow),
            "denylist": sorted(self.deny),
            "mandatory_denylist": sorted(MANDATORY_DENY),
            "allowed_roots": [str(root) for root in self.roots],
            "shell": False,
            "env_allowlist": list(self.env_allowlist),
            "timeout_seconds": self.timeout,
            "exact_commands": [list(item) for item in self.exact_commands],
        }


class ShellRun(CoreTool):
    name = "shell.run"
    description = "Run an allowlisted command in a sandboxed subprocess (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": 500},
            "cwd": {"type": "string"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, sandbox: ShellSandbox) -> None:
        self.sandbox = sandbox

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {"tool": self.name, "dry_run": True, "plan": self.sandbox.plan(kwargs.get("command", ""), kwargs.get("cwd")), "side_effects": "none"}

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "command")
        return self.sandbox.run(self.text(kwargs.get("command")), kwargs.get("cwd"))


class ShellStatus(CoreTool):
    name = "shell.status"
    description = "Show the shell sandbox policy without executing anything."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, sandbox: ShellSandbox) -> None:
        self.sandbox = sandbox

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self.sandbox.status()
