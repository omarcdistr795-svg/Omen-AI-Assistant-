"""Coding/DevOps and small data-analysis helpers built on safe primitives."""

from __future__ import annotations

import ast
import csv
import io
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from ...safety.redaction import redact_value
from .base import CoreTool, CoreToolError, truncate
from .gittools import GitRunner
from .shell import ShellSandbox

_SUFFIX_LANGUAGE = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".sh": "Shell",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
}


class CodeInspect(CoreTool):
    name = "code.inspect"
    description = "Explain the structure of an allowed source file using the AST (read-only)."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, policy: Any) -> None:
        self.policy = policy

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "path")
        target = self.policy.resolve(kwargs["path"])
        source = self.policy.read_text(target)
        if target.suffix.lower() != ".py":
            return {"path": str(target), "language": "text", "lines": len(source.splitlines()), "note": "AST inspection supports Python files only"}
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise CoreToolError(f"Could not parse {target}: {exc}") from exc
        imports: list[str] = []
        functions: list[str] = []
        classes: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
        return {
            "path": str(target),
            "language": "Python",
            "lines": len(source.splitlines()),
            "imports": sorted(set(imports)),
            "functions": sorted(set(functions)),
            "classes": sorted(set(classes)),
            "todos": source.upper().count("TODO"),
            "method": "ast",
        }


class RepoContext(CoreTool):
    name = "repo.context"
    description = "Detect repository branch, recent changes, languages, TODOs and docs (read-only)."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {"repo": {"type": "string"}},
        "additionalProperties": False,
    }

    def __init__(self, runner: GitRunner, policy: Any) -> None:
        self.runner = runner
        self.policy = policy

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        repo = self.runner.repo(kwargs.get("repo"))
        branch_output = self.runner.run(repo, ["rev-parse", "--abbrev-ref", "HEAD"])["stdout"].splitlines()
        branch = branch_output[0] if branch_output else ""
        recent = self.runner.run(repo, ["log", "--oneline", "-n", "5"])["stdout"]
        status = self.runner.run(repo, ["status", "--short", "--branch"])["stdout"]
        languages: Counter[str] = Counter()
        todos = 0
        for path in repo.rglob("*"):
            if not path.is_file() or path.is_symlink() or ".git" in path.parts:
                continue
            if not (path.resolve(strict=False) == repo or repo in path.resolve(strict=False).parents):
                continue
            language = _SUFFIX_LANGUAGE.get(path.suffix.lower())
            if language:
                languages[language] += 1
            if path.stat().st_size <= 1_048_576 and path.suffix.lower() in (".py", ".md", ".ts", ".js"):
                try:
                    todos += path.read_text(encoding="utf-8").upper().count("TODO")
                except (UnicodeDecodeError, OSError):
                    continue
        docs = [name for name in ("README.md", "README", "docs", "CONTRIBUTING.md") if (repo / name).exists()]
        return {
            "repo": str(repo),
            "branch": branch,
            "recent_commits": recent.splitlines(),
            "status": status.splitlines(),
            "languages": dict(languages.most_common()),
            "todos": todos,
            "documentation": docs,
        }


class TestsRun(CoreTool):
    name = "tests.run"
    description = "Run a pre-registered test command in the sandbox (requires approval)."
    permission_class = "low_risk_write"
    _schema = {
        "type": "object",
        "properties": {"repo": {"type": "string"}},
        "additionalProperties": False,
    }

    def __init__(self, sandbox: ShellSandbox) -> None:
        self.sandbox = sandbox

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tool": self.name,
            "dry_run": True,
            "commands": [list(item) for item in self.sandbox.exact_commands],
            "side_effects": "none",
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        results = []
        for argv in self.sandbox.exact_commands:
            results.append(self.sandbox.run_exact(list(argv), kwargs.get("repo")))
        if not results:
            raise CoreToolError("No test commands are configured in core_tools.tests.commands")
        return {
            "runs": results,
            "all_passed": all(item["exit_code"] == 0 for item in results),
            "note": "Failures are reported verbatim; never hidden or skipped.",
        }


class DataSummarise(CoreTool):
    name = "data.summarise"
    description = "Summarise numeric columns in an allowed CSV or JSON file (read-only)."
    permission_class = "read_only"
    _schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, policy: Any) -> None:
        self.policy = policy

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.require(kwargs, "path")
        target = self.policy.resolve(kwargs["path"])
        raw = self.policy.read_text(target)
        suffix = target.suffix.lower()
        if suffix == ".json":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CoreToolError(f"Invalid JSON: {exc}") from exc
            rows = parsed if isinstance(parsed, list) else [parsed]
            if not all(isinstance(row, dict) for row in rows):
                raise CoreToolError("JSON data must be an object or a list of objects")
        elif suffix == ".csv":
            try:
                rows = list(csv.DictReader(io.StringIO(raw)))
            except csv.Error as exc:
                raise CoreToolError(f"Invalid CSV: {exc}") from exc
        else:
            raise CoreToolError("data.summarise supports .csv and .json files only")
        columns: dict[str, dict[str, Any]] = {}
        for row in rows:
            for key, value in row.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                entry = columns.setdefault(key, {"count": 0, "total": 0.0, "min": number, "max": number})
                entry["count"] += 1
                entry["total"] += number
                entry["min"] = min(entry["min"], number)
                entry["max"] = max(entry["max"], number)
        summary = {
            key: {
                "count": value["count"],
                "mean": round(value["total"] / value["count"], 4),
                "min": value["min"],
                "max": value["max"],
            }
            for key, value in columns.items()
        }
        return {
            "path": str(target),
            "rows": len(rows),
            "columns": redact_value(summary),
            "format": suffix.lstrip("."),
        }
