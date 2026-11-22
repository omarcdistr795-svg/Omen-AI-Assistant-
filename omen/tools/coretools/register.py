"""Wire Phase 3 tools into the existing Phase 1 ToolRegistry.

Nothing in Phase 1/2 is rewritten: this module only registers new tools and
adds their names to the registry's configured allowlist. Policy, approval and
audit remain entirely owned by the Phase 1 safety layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..registry import ToolRegistry, ToolSpec
from .base import PathPolicy
from .connectors import ConnectorRegistry
from .devtools import CodeInspect, DataSummarise, RepoContext, TestsRun
from .files import FileDelete, FileDraft, FileList, FileRead, FileSearch, FileSummarise, FileUpdate
from .gittools import GitBranches, GitCommit, GitDiff, GitHistory, GitPrDraft, GitRunner, GitStatus
from .localdata import DOMAINS, LocalDataStore, LocalRecordTool
from .rollback import RollbackPlan, RollbackStore, RollbackUndo
from .shell import ShellRun, ShellSandbox, ShellStatus
from .systemtools import SystemController, SystemMedia, SystemOpenApp, SystemStatus, SystemWindow
from .web import WebCompare, WebProvider, WebRead, WebSaveNote, WebSearch, WebSummarise

_READ_ACTIONS = frozenset({"get", "list", "search"})
_WRITE_ACTIONS = frozenset({"create", "update", "delete", "complete"})

_LOCAL_ACTIONS = ("create", "get", "list", "search", "update", "delete")
CORE_TOOL_NAMES: tuple[str, ...] = (
    "web.search",
    "web.read",
    "web.summarise",
    "web.compare",
    "web.save_note",
    "files.read",
    "files.search",
    "files.summarise",
    "files.list",
    "files.draft",
    "files.update",
    "files.delete",
    *(f"{domain}.{action}" for domain in DOMAINS for action in _LOCAL_ACTIONS),
    "reminders.complete",
    "tasks.complete",
    "shell.status",
    "shell.run",
    "git.status",
    "git.diff",
    "git.history",
    "git.branches",
    "git.commit",
    "git.pr_draft",
    "system.status",
    "system.open_app",
    "system.media",
    "system.window",
    "rollback.plan",
    "rollback.undo",
    "code.inspect",
    "repo.context",
    "tests.run",
    "data.summarise",
)


def _spec(tool: Any, *, write: bool, side_effects: str, rollback: str = "not required") -> ToolSpec:
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        permission_class=tool.permission_class,
        timeout_seconds=10.0,
        max_retries=0 if write else 1,
        backoff_seconds=0.25,
        idempotent=not write,
        side_effects=side_effects,
        rollback=rollback,
        input_schema=tool.schema(),
    )


def register_core_tools(app: Any) -> dict[str, Any]:
    """Register every Phase 3 tool on ``app.tools`` and return a manifest."""
    config = app.config
    if not config.get("core_tools.enabled", True):
        return {"enabled": False, "tools": []}
    registry: ToolRegistry = app.tools
    root = Path(app.root).resolve()

    connectors = ConnectorRegistry(config)
    file_cfg = config.get("core_tools.files") or {}
    storage_cfg = config.get("core_tools.storage") or {}
    drafts_dir = (root / str(storage_cfg.get("drafts_dir", "state/phase3/drafts"))).resolve()
    quarantine_dir = (root / str(storage_cfg.get("quarantine_dir", "state/phase3/quarantine"))).resolve()
    drafts_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    path_policy = PathPolicy(
        list(file_cfg.get("allowed_roots") or ["."]),
        base=root,
        max_file_bytes=int(file_cfg.get("max_file_bytes", 1_048_576)),
        max_output_chars=int(file_cfg.get("max_output_chars", 20_000)),
    )

    rollback_store = RollbackStore(config, root)
    local_store = LocalDataStore(config, root)
    web = WebProvider(config, connectors, budget=getattr(app, "budget", None))
    shell = ShellSandbox(config, root)
    git = GitRunner(config, root)
    system = SystemController(config)

    tools: list[tuple[Any, bool, str, str]] = []

    # Web -----------------------------------------------------------------
    tools.extend(
        [
            (WebSearch(web), False, "network read via opt-in connector or offline mock", "not required"),
            (WebRead(web), False, "network read via opt-in connector or offline mock", "not required"),
            (WebSummarise(web), False, "none", "not required"),
            (WebCompare(web), False, "none", "not required"),
            (WebSaveNote(web, local_store, rollback_store), True, "local note created", "undo removes the note"),
        ]
    )

    # Files ---------------------------------------------------------------
    file_args = (path_policy, rollback_store, drafts_dir, quarantine_dir)
    tools.extend(
        [
            (FileRead(*file_args), False, "none", "not required"),
            (FileSearch(*file_args), False, "none", "not required"),
            (FileSummarise(*file_args), False, "none", "not required"),
            (FileList(*file_args), False, "none", "not required"),
            (FileDraft(*file_args), True, "draft file created", "undo quarantines the draft"),
            (FileUpdate(*file_args), True, "file overwritten after backup", "undo restores backup"),
            (FileDelete(*file_args), True, "file moved to quarantine", "undo moves it back"),
        ]
    )

    # Notes, reminders, tasks, calendar ----------------------------------
    for domain in DOMAINS:
        for action in ("create", "get", "list", "search", "update", "delete"):
            if action == "delete":
                permission = "high_risk"
                side_effects = f"{domain} record soft-deleted"
            elif action in _WRITE_ACTIONS:
                permission = "low_risk_write"
                side_effects = f"{domain} record changed"
            else:
                permission = "read_only"
                side_effects = "none"
            rollback_note = "undo restores the previous record" if action in _WRITE_ACTIONS else "not required"
            tools.append(
                (
                    LocalRecordTool(
                        domain,
                        action,
                        local_store,
                        rollback_store,
                        permission_class=permission,
                        connector=connectors.get(domain),
                    ),
                    action in _WRITE_ACTIONS,
                    side_effects,
                    rollback_note,
                )
            )
    for domain in ("reminders", "tasks"):
        tools.append(
            (
                LocalRecordTool(
                    domain,
                    "complete",
                    local_store,
                    rollback_store,
                    connector=connectors.get(domain),
                ),
                True,
                f"{domain} record completed",
                "undo restores the previous status",
            )
        )

    # Shell / Git / System -----------------------------------------------
    tools.extend(
        [
            (ShellStatus(shell), False, "none", "not required"),
            (ShellRun(shell), True, "sandboxed subprocess executed", "not reversible"),
            (GitStatus(git), False, "none", "not required"),
            (GitDiff(git), False, "none", "not required"),
            (GitHistory(git), False, "none", "not required"),
            (GitBranches(git), False, "none", "not required"),
            (GitCommit(git, rollback_store), True, "local commit created", "undo uses git reset --soft"),
            (GitPrDraft(git, rollback_store, drafts_dir), True, "local PR draft written", "undo quarantines the draft"),
            (SystemStatus(system), False, "none", "not required"),
            (SystemOpenApp(system), True, "application launched", "close the application manually"),
            (SystemMedia(system), True, "media playback changed", "play/pause is reversible"),
            (SystemWindow(system), True, "window state changed", "window actions are not reversible"),
        ]
    )

    # Rollback and dev helpers -------------------------------------------
    tools.extend(
        [
            (RollbackPlan(rollback_store), False, "none", "not required"),
            (RollbackUndo(rollback_store, policy=path_policy, local_store=local_store, quarantine_dir=quarantine_dir, git_roots=git.roots, connectors=connectors), True, "recorded change reverted", "one-shot plan"),
            (CodeInspect(path_policy), False, "none", "not required"),
            (RepoContext(git, path_policy), False, "none", "not required"),
            (TestsRun(shell), True, "test command executed", "not reversible"),
            (DataSummarise(path_policy), False, "none", "not required"),
        ]
    )

    registered: list[str] = []
    for tool, write, side_effects, rollback_note in tools:
        registry.register(tool, spec=_spec(tool, write=write, side_effects=side_effects, rollback=rollback_note))
        registered.append(tool.name)

    configured = tuple(config.get("core_tools.allowlist") or ())
    registry.allowlist = tuple(dict.fromkeys((*registry.allowlist, *configured)))

    return {
        "enabled": True,
        "tools": registered,
        "connectors": connectors.status(),
        "local_data": local_store.status(),
        "rollback_plans": len(rollback_store.list(200)),
        "sandbox": shell.status(),
        "web_mode": "mock_offline" if connectors.get("web_search") is None else "connector",
    }
