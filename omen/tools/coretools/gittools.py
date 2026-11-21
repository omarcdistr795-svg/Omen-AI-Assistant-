"""Read-only Git inspection plus approval-gated local commits and PR drafts."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, truncate

ALLOWED_SUBCOMMANDS = frozenset({"status", "diff", "log", "branch", "show", "rev-parse", "add", "commit"})
FORBIDDEN_TOKENS = frozenset(
    {
        "push",
        "fetch",
        "pull",
        "reset",
        "clean",
        "remote",
        "config",
        "rebase",
        "merge",
        "stash",
        "gc",
        "--force",
        "--hard",
        "-c",
        "--exec",
        "--upload-pack",
    }
)


class GitRunner:
    def __init__(self, config: Any, root: Path | str) -> None:
        base = Path(root).resolve()
        repos = list(config.get("core_tools.git.allowed_repos") or ["."])
        self.roots: tuple[Path, ...] = tuple(
            (base / str(item)).resolve() if not Path(str(item)).is_absolute() else Path(str(item)).resolve()
            for item in repos
        )
        if not self.roots:
            raise CoreToolError("Git tools need at least one allowed repository")
        self.max_output = int(config.get("core_tools.git.max_output_chars", 20_000))

    def repo(self, value: Any = None) -> Path:
        raw = str(value or self.roots[0])
        path = Path(raw)
        if not path.is_absolute():
            path = self.roots[0] / path
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in self.roots):
            raise CoreToolError(f"Repository is outside the allowed roots: {raw}")
        if not (resolved / ".git").exists():
            raise CoreToolError(f"Not a Git repository: {resolved}")
        return resolved

    def pathspec(self, repo: Path, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw.startswith("-"):
            raise CoreToolError("Path must be a relative path inside the repository")
        candidate = (repo / raw).resolve()
        if not (candidate == repo or repo in candidate.parents):
            raise CoreToolError(f"Path escapes the repository: {raw}")
        return str(candidate.relative_to(repo))

    def run(self, repo: Path, args: list[str], *, check: bool = True, redact: bool = True) -> dict[str, Any]:
        if not args or args[0] not in ALLOWED_SUBCOMMANDS:
            raise CoreToolError(f"Git subcommand is not allowed: {args[0] if args else ''}")
        for token in args:
            if token in FORBIDDEN_TOKENS:
                raise CoreToolError(f"Git flag is forbidden by policy: {token}")
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ") if key in os.environ}
        env["GIT_TERMINAL_PROMPT"] = "0"
        completed = subprocess.run(
            ["git", "--no-pager", "--no-optional-locks", *args],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if check and completed.returncode != 0:
            raise CoreToolError(f"git {' '.join(args)} failed: {completed.stderr.strip() or completed.returncode}")
        mask = redact_value if redact else (lambda value: value)
        return {
            "repo": str(repo),
            "command": mask(["git", *args]),
            "exit_code": completed.returncode,
            "stdout": mask(truncate(completed.stdout, self.max_output)),
            "stderr": mask(truncate(completed.stderr, self.max_output)),
        }

    def head(self, repo: Path) -> str:
        # A commit hash is not a secret; redacting it can corrupt rollback ids.
        head = self.run(repo, ["rev-parse", "HEAD"], redact=False)["stdout"].strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", head):
            raise CoreToolError("Git returned an invalid commit hash")
        return head


class _GitTool(CoreTool):
    def __init__(self, runner: GitRunner) -> None:
        self.runner = runner


class GitStatus(_GitTool):
    name = "git.status"
    description = "Show branch and working tree status for an allowed repository."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {"repo": {"type": "string"}}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self.runner.run(self.runner.repo(kwargs.get("repo")), ["status", "--short", "--branch"])


class GitDiff(_GitTool):
    name = "git.diff"
    description = "Show a Git diff for an allowed repository."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "staged": {"type": "boolean"},
            "paths": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        repo = self.runner.repo(kwargs.get("repo"))
        args = ["diff"]
        if kwargs.get("staged"):
            args.append("--staged")
        paths = kwargs.get("paths") or []
        if not isinstance(paths, list):
            raise CoreToolError("paths must be a list")
        if paths:
            args.append("--")
            args.extend(self.runner.pathspec(repo, item) for item in paths)
        return self.runner.run(repo, args)


class GitHistory(_GitTool):
    name = "git.history"
    description = "Show recent commits for an allowed repository."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "path": {"type": "string"},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        repo = self.runner.repo(kwargs.get("repo"))
        args = ["log", "--oneline", "-n", str(max(1, min(int(kwargs.get("limit") or 10), 50)))]
        if kwargs.get("path"):
            args.extend(["--", self.runner.pathspec(repo, kwargs.get("path"))])
        return self.runner.run(repo, args)


class GitBranches(_GitTool):
    name = "git.branches"
    description = "List local and remote branches for an allowed repository."
    permission_class = "read_only"
    _schema = {"type": "object", "properties": {"repo": {"type": "string"}}, "additionalProperties": False}

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self.runner.run(self.runner.repo(kwargs.get("repo")), ["branch", "-a", "-vv"])


class GitCommit(_GitTool):
    name = "git.commit"
    description = "Stage explicit paths and create a local commit with an undo plan (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "message": {"type": "string", "minLength": 1, "maxLength": 500},
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["message", "paths"],
        "additionalProperties": False,
    }

    def __init__(self, runner: GitRunner, rollback: Any) -> None:
        super().__init__(runner)
        self.rollback = rollback

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tool": self.name,
            "dry_run": True,
            "would_stage": redact_value(kwargs.get("paths") or []),
            "would_commit": self.text(kwargs.get("message")),
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "message")
        paths = kwargs.get("paths")
        if not isinstance(paths, list) or not paths:
            raise CoreToolError("paths must be a non-empty list")
        repo = self.runner.repo(kwargs.get("repo"))
        before = self.runner.head(repo)
        specs = [self.runner.pathspec(repo, item) for item in paths]
        self.runner.run(repo, ["add", "--", *specs])
        committed = self.runner.run(repo, ["commit", "-m", self.text(kwargs.get("message"))], check=False)
        if committed["exit_code"] != 0:
            raise CoreToolError(f"git commit failed; index may contain staged changes: {committed['stderr']}")
        after = self.runner.head(repo)
        plan = self.rollback.record(
            "git_commit",
            self.name,
            str(repo),
            {"repo": str(repo), "before_head": before, "commit": after},
        )
        return {
            **committed,
            "before_head": before,
            "commit": after,
            "rollback_plan_id": plan,
            "rollback": "undo uses git reset --soft to the previous HEAD",
        }


class GitPrDraft(_GitTool):
    name = "git.pr_draft"
    description = "Write a local pull-request draft from a repository diff (no push, no API)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "body": {"type": "string", "maxLength": 20000},
            "base": {"type": "string", "maxLength": 100},
        },
        "required": ["title"],
        "additionalProperties": False,
    }

    def __init__(self, runner: GitRunner, rollback: Any, drafts_dir: Path) -> None:
        super().__init__(runner)
        self.rollback = rollback
        self.drafts_dir = drafts_dir

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {"tool": self.name, "dry_run": True, "would_create": "local PR draft file", "side_effects": "none"}

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "title")
        repo = self.runner.repo(kwargs.get("repo"))
        diff = self.runner.run(repo, ["diff", "--stat"])["stdout"]
        safe = self.clean_data(
            {"title": self.text(kwargs.get("title")), "body": self.text(kwargs.get("body")), "base": self.text(kwargs.get("base"))}
        )
        content = (
            f"# PR draft: {safe['title']}\n\n"
            f"Base branch: {safe.get('base') or '(unspecified)'}\n\n"
            f"## Summary\n\n{safe.get('body') or '(no summary provided)'}\n\n"
            "## Local diff stat (informational)\n\n```\n" + diff + "\n```\n"
        )
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        target = self.drafts_dir / f"pr_draft_{len(list(self.drafts_dir.glob('pr_draft_*.md'))) + 1}.md"
        target.write_text(content, encoding="utf-8")
        plan = self.rollback.record("file_remove", self.name, str(target), {"path": str(target)})
        return {
            "path": str(target),
            "title": safe["title"],
            "pushed": False,
            "api_called": False,
            "rollback_plan_id": plan,
            "rollback": "undo moves the local draft into quarantine",
        }
