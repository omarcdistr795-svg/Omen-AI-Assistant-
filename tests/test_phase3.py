"""Phase 3 core tools: safety, happy paths, failure paths and rollback."""

from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from omen.app import OmenApp
from omen.config import Config, load_config
from omen.safety.policy import VALID_CLASSES
from omen.tools.coretools import CORE_TOOL_NAMES, PathPolicy, PathPolicyError
from omen.tools.coretools.base import CoreToolError
from omen.tools.coretools.connectors import ConnectorRegistry, MockConnector
from omen.tools.coretools.gittools import GitRunner
from omen.tools.coretools.localdata import DOMAINS
from omen.tools.coretools.shell import ShellSandbox


def _patch_config(**patches: object) -> Config:
    base = load_config()
    sections = copy.deepcopy(base.sections)
    for dotted, value in patches.items():
        node = sections
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(sections=sections, source=base.source)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=omen@example.invalid", "-c", "user.name=Omen Test", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


class Phase3Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="omen-phase3-")
        self.root = Path(self._tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "src").mkdir()
        (self.root / "README.md").write_text("# Demo repository\nLocal-first notes.\n", encoding="utf-8")
        (self.root / "src" / "sample.py").write_text("import os\n\ndef hello(name):\n    return name\n", encoding="utf-8")
        (self.root / "data.csv").write_text("name,value\na,1\nb,3\n", encoding="utf-8")
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "omen@example.invalid")
        _git(self.root, "config", "user.name", "Omen Test")
        _git(self.root, "add", "README.md", "src/sample.py")
        _git(self.root, "commit", "-q", "-m", "initial commit")
        self.app = OmenApp(config=self.make_config(), root=self.root, output=lambda _text: None)

    def tearDown(self) -> None:
        self.app.close()
        self._tmp.cleanup()

    def make_config(self, **patches: object) -> Config:
        # Tests issue many calls in one minute; production keeps the configured cap.
        return _patch_config(**{"tools.rate_limit_per_minute": 2000, **patches})

    def run_tool(self, name: str, params: dict | None = None, *, approved: bool = False):
        params = dict(params or {})
        result = self.app.run_tool(name, params)
        if approved and result.status == "pending_approval":
            token = self.app.approvals.pending[result.token_id]
            self.app.approve(result.token_id)
            result = self.app.run_tool(name, params, approval_token=token)
        return result

    def new_app(self, **patches: object) -> OmenApp:
        return OmenApp(config=self.make_config(**patches), root=self.root, output=lambda _text: None)


class RegistrationAndSafetyTests(Phase3Case):
    def test_all_core_tools_registered_allowlisted_and_typed(self) -> None:
        specs = {spec.name: spec for spec in self.app.tools.list()}
        self.assertTrue(set(CORE_TOOL_NAMES).issubset(specs))
        for name in CORE_TOOL_NAMES:
            spec = specs[name]
            self.assertIn(spec.permission_class, VALID_CLASSES)
            self.assertIn(name, self.app.tools.allowlist)
            self.assertIn("parameters", self.app.tools.schemas()[name])
        self.assertGreaterEqual(len(specs), 50)

    def test_write_tools_require_approval_read_tools_do_not(self) -> None:
        for spec in self.app.tools.list():
            if spec.name not in CORE_TOOL_NAMES:
                continue
            if spec.permission_class == "read_only":
                self.assertEqual(spec.permission_class, "read_only")
                self.assertNotIn(spec.permission_class, ("low_risk_write", "external_write", "high_risk"))
            else:
                self.assertEqual(spec.max_retries, 0, f"{spec.name} must not retry a side effect")

    def test_approval_flow_is_exact_and_single_use(self) -> None:
        params = {"filename": "approved.md", "content": "written after approval"}
        first = self.run_tool("files.draft", params)
        self.assertEqual(first.status, "pending_approval")
        token = self.app.approvals.pending[first.token_id]
        self.app.approve(first.token_id)
        result = self.app.run_tool("files.draft", params, approval_token=token)
        self.assertEqual(result.status, "ok")
        self.assertTrue((self.root / "state/phase3/drafts/approved.md").exists())
        replay = self.app.run_tool("files.draft", params, approval_token=token)
        self.assertEqual(replay.status, "denied")

    def test_dry_run_has_no_side_effects(self) -> None:
        result = self.app.tools.dry_run("files.draft", params={"filename": "dry.md", "content": "x"})
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.dry_run)
        self.assertFalse((self.root / "state/phase3/drafts/dry.md").exists())

    def test_always_blocked_phrases_are_rejected_before_execution(self) -> None:
        result = self.run_tool("notes.create", {"title": "payment execution plan", "body": "x"})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.decision, "blocked")

    def test_kill_switch_blocks_core_tools(self) -> None:
        self.app.kill("phase 3 test")
        result = self.run_tool("web.search", {"query": "local"})
        self.assertEqual(result.status, "blocked")

    def test_all_tool_calls_are_audited(self) -> None:
        self.run_tool("web.search", {"query": "safety"})
        events = self.app.history(tool="web.search")
        self.assertTrue(events)
        self.assertTrue(self.app.audit.verify_integrity())

    def test_every_write_tool_requires_approval(self) -> None:
        samples: dict[str, dict] = {
            "web.save_note": {"title": "Research", "url": "https://omen-local.invalid/guides/tool-safety"},
            "files.draft": {"filename": "approval.md", "content": "x"},
            "files.update": {"path": "README.md", "content": "x"},
            "files.delete": {"path": "README.md"},
            "shell.run": {"command": "pwd"},
            "git.commit": {"message": "test", "paths": ["README.md"]},
            "git.pr_draft": {"title": "Draft"},
            "system.open_app": {"name": "unknown"},
            "system.media": {"action": "play"},
            "system.window": {"action": "clients"},
            "rollback.undo": {"plan_id": "undo_missing"},
            "tests.run": {},
        }
        for domain in DOMAINS:
            samples[f"{domain}.create"] = {"title": "item"}
            samples[f"{domain}.update"] = {"id": "record_missing", "changes": {"title": "x"}}
            samples[f"{domain}.delete"] = {"id": "record_missing"}
        samples["reminders.complete"] = {"id": "record_missing"}
        samples["tasks.complete"] = {"id": "record_missing"}
        specs = {spec.name: spec for spec in self.app.tools.list()}
        writes = [name for name in CORE_TOOL_NAMES if specs[name].permission_class != "read_only"]
        self.assertEqual(set(writes), set(samples), "Every write tool needs a sample action")
        for name in writes:
            with self.subTest(tool=name):
                result = self.app.run_tool(name, samples[name])
                self.assertEqual(result.status, "pending_approval", name)
                self.assertTrue(result.token_id)

    def test_every_tool_has_a_failure_path(self) -> None:
        # Kill switch is a universal failure path enforced before execution.
        self.app.kill("universal failure path test")
        for name in CORE_TOOL_NAMES:
            with self.subTest(tool=name):
                result = self.app.run_tool(name, {})
                self.assertEqual(result.status, "blocked", name)
                self.assertEqual(result.decision, "blocked")


class PathAndConnectorTests(Phase3Case):
    def test_path_policy_rejects_escape_symlink_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omen-p3-paths-") as td:
            root = Path(td)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside.txt"
            outside.write_text("secret location", encoding="utf-8")
            (allowed / "link.txt").symlink_to(outside)
            policy = PathPolicy(["allowed"], base=root, max_file_bytes=10)
            with self.assertRaises(PathPolicyError):
                policy.resolve("../outside.txt")
            with self.assertRaises(PathPolicyError):
                policy.resolve("allowed/link.txt", must_exist=True)
            big = allowed / "big.txt"
            big.write_text("x" * 20, encoding="utf-8")
            with self.assertRaises(PathPolicyError):
                policy.resolve("allowed/big.txt")
            self.assertTrue(policy.resolve(".").is_dir())

    def test_connector_registry_is_offline_by_default(self) -> None:
        registry = ConnectorRegistry(load_config())
        self.assertEqual(registry.status(), {})
        self.assertIsNone(registry.get("web_search"))

    def test_external_connector_blocked_with_zero_budget(self) -> None:
        config = _patch_config(
            **{
                "core_tools.connectors": {
                    "remote": {"enabled": True, "type": "mock", "kind": "calendar", "external": True}
                }
            }
        )
        with self.assertRaises(CoreToolError) as ctx:
            ConnectorRegistry(config)
        self.assertIn("BLOCKED-EXTERNAL", str(ctx.exception))

    def test_mock_connector_is_usable(self) -> None:
        config = _patch_config(
            **{
                "cost.enabled": True,
                "cost.monthly_budget_usd": 1.0,
                "core_tools.connectors": {
                    "remote": {"enabled": True, "type": "mock", "kind": "calendar", "external": True}
                },
            }
        )
        registry = ConnectorRegistry(config)
        connector = registry.get("calendar")
        self.assertIsNotNone(connector)
        self.assertIn("calendar", registry.status())
        self.assertIsInstance(connector, MockConnector)
        self.assertTrue(connector.health()["ok"])

    def test_local_record_connector_can_be_used_with_rollback(self) -> None:
        from omen.tools.coretools.connectors import ConnectorRegistry

        config = _patch_config(
            **{
                "core_tools.connectors": {
                    "calendar_mock": {
                        "enabled": True,
                        "type": "mock",
                        "kind": "calendar",
                        "external": False,
                        "responses": {
                            "create": {"record": {"id": "cal_remote_1", "title": "Synced meeting"}},
                            "get": {"record": {"id": "cal_remote_1", "title": "Synced meeting"}},
                            "remove": {"removed": "cal_remote_1"},
                        },
                    }
                }
            }
        )
        app = OmenApp(config=config, root=self.root, output=lambda _: None)
        result = app.run_tool("calendar.create", {"title": "Synced meeting"})
        token = app.approvals.pending[result.token_id]
        app.approve(result.token_id)
        done = app.run_tool(
            "calendar.create", {"title": "Synced meeting"}, approval_token=token
        )
        self.assertEqual(done.status, "ok")
        self.assertEqual(done.output["storage"], "calendar_mock")
        self.assertFalse(done.output["external"])
        undo = app.run_tool("rollback.undo", {"plan_id": done.output["rollback_plan_id"]})
        undo_token = app.approvals.pending[undo.token_id]
        app.approve(undo.token_id)
        undone = app.run_tool(
            "rollback.undo", {"plan_id": done.output["rollback_plan_id"]}, approval_token=undo_token
        )
        self.assertEqual(undone.status, "ok")
        app.close()


class WebToolTests(Phase3Case):
    def test_search_summarise_and_cite(self) -> None:
        result = self.run_tool("web.search", {"query": "local-first", "limit": 3})
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.output["results"])
        self.assertEqual(result.output["source"], "mock_offline_corpus")
        self.assertTrue(result.output["blocked_external"])
        url = result.output["results"][0]["url"]
        page = self.run_tool("web.read", {"url": url})
        self.assertEqual(page.status, "ok")
        self.assertIn("local", page.output["text"].lower())
        summary = self.run_tool("web.summarise", {"url": url, "query": "approval", "max_sentences": 2})
        self.assertEqual(summary.status, "ok")
        self.assertIn("[1]", summary.output["summary"])
        self.assertEqual(summary.output["citations"][0]["url"], url)

    def test_compare_requires_two_sources_and_reports_citations(self) -> None:
        bad = self.run_tool("web.compare", {"urls": ["https://omen-local.invalid/guides/local-first"]})
        self.assertEqual(bad.status, "error")
        urls = [
            "https://omen-local.invalid/guides/local-first",
            "https://omen-local.invalid/research/memory-notes",
        ]
        result = self.run_tool("web.compare", {"urls": urls, "query": "budget"})
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.output["claims"])
        self.assertEqual(len(result.output["sources"]), 2)

    def test_unknown_page_fails_loudly(self) -> None:
        result = self.run_tool("web.read", {"url": "https://not-in-corpus.invalid/page"})
        self.assertEqual(result.status, "error")

    def test_external_connector_respects_budget_cap(self) -> None:
        from omen.cost.budget import BudgetController
        from omen.tools.coretools.web import WebProvider

        config = self.make_config(
            **{
                "cost.enabled": True,
                "cost.monthly_budget_usd": 1.0,
                "core_tools.web.search_connector": "remote",
            }
        )
        budget = BudgetController(config, state_path=self.root / "cost_state.json")
        connector = MockConnector(
            "remote",
            "web_search",
            responses={"search": {"results": [{"title": "Live", "url": "https://example.invalid/a", "snippet": "x"}]}},
            external=True,
        )

        class StubConnectors:
            def get(self, kind: str) -> object:
                return connector if kind == "web_search" else None

        provider = WebProvider(config, StubConnectors(), budget)
        live = provider.search("anything")
        self.assertTrue(live["external"])
        self.assertEqual(live["source"], "remote")
        budget.record(provider="remote", local=False, cost_usd=2.0)
        with self.assertRaises(CoreToolError):
            provider.search("anything")

    def test_save_research_note_requires_approval_and_is_undoable(self) -> None:
        url = "https://omen-local.invalid/guides/tool-safety"
        params = {"title": "Tool safety research", "url": url}
        pending = self.run_tool("web.save_note", params)
        self.assertEqual(pending.status, "pending_approval")
        done = self.run_tool("web.save_note", params, approved=True)
        self.assertEqual(done.status, "ok")
        note_id = done.output["note"]["id"]
        self.assertTrue(done.output["citations"])
        undo = self.run_tool("rollback.undo", {"plan_id": done.output["rollback_plan_id"]}, approved=True)
        self.assertEqual(undo.status, "ok")
        notes = self.run_tool("notes.list", {})
        self.assertNotIn(note_id, [item["id"] for item in notes.output["records"]])


class FileToolTests(Phase3Case):
    def test_draft_update_read_search_summarise_list_and_undo(self) -> None:
        draft = self.run_tool("files.draft", {"filename": "report.md", "content": "Omen keeps notes local. Safety is enforced."}, approved=True)
        self.assertEqual(draft.status, "ok")
        path = draft.output["path"]
        self.assertEqual(self.run_tool("files.read", {"path": path}).status, "ok")
        found = self.run_tool("files.search", {"query": "Safety", "path": "state/phase3/drafts"})
        self.assertEqual(found.status, "ok")
        self.assertTrue(found.output["matches"])
        summary = self.run_tool("files.summarise", {"path": path})
        self.assertEqual(summary.status, "ok")
        listing = self.run_tool("files.list", {"path": "state/phase3/drafts"})
        self.assertEqual(listing.status, "ok")
        update = self.run_tool("files.update", {"path": path, "content": "Replaced content."}, approved=True)
        self.assertEqual(update.status, "ok")
        self.assertEqual(self.run_tool("files.read", {"path": path}).output["content"], "Replaced content.")
        undo = self.run_tool("rollback.undo", {"plan_id": update.output["rollback_plan_id"]}, approved=True)
        self.assertEqual(undo.status, "ok")
        self.assertIn("Safety", self.run_tool("files.read", {"path": path}).output["content"])

    def test_delete_moves_to_quarantine_and_undo_restores(self) -> None:
        target = self.root / "README.md"
        result = self.run_tool("files.delete", {"path": str(target)}, approved=True)
        self.assertEqual(result.status, "ok")
        self.assertFalse(target.exists())
        self.assertTrue(Path(result.output["quarantine"]).exists())
        undo = self.run_tool("rollback.undo", {"plan_id": result.output["rollback_plan_id"]}, approved=True)
        self.assertEqual(undo.status, "ok")
        self.assertTrue(target.exists())

    def test_search_skips_symlinks_that_escape_allowed_roots(self) -> None:
        outside = self.root.parent / "omen_phase3_outside.txt"
        outside.write_text("root:x:0:0 unique-marker\n", encoding="utf-8")
        link = self.root / "escape_link.txt"
        link.symlink_to(outside)
        try:
            result = self.run_tool("files.search", {"query": "unique-marker"})
            self.assertEqual(result.status, "ok")
            self.assertFalse(result.output["matches"])
        finally:
            link.unlink()
            outside.unlink()

    def test_failure_paths(self) -> None:
        self.assertEqual(self.run_tool("files.read", {"path": "../outside.txt"}).status, "error")
        self.assertEqual(self.run_tool("files.draft", {"filename": "missing.md"}, approved=True).status, "error")
        self.assertEqual(self.run_tool("files.draft", {"filename": "report.md", "content": "x"}).status, "pending_approval")
        approved = self.run_tool("files.draft", {"filename": "report.md", "content": "x"}, approved=True)
        self.assertEqual(approved.status, "ok")
        duplicate = self.run_tool("files.draft", {"filename": "report.md", "content": "y"}, approved=True)
        self.assertEqual(duplicate.status, "error")
        secret = self.run_tool("files.draft", {"filename": "secret.md", "content": "api_key=abcdefghijklmnop"}, approved=True)
        self.assertEqual(secret.status, "error")


class LocalDataToolTests(Phase3Case):
    def test_every_domain_crud_search_and_undo(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                created = self.run_tool(f"{domain}.create", {"title": f"{domain} item", "body": "details"}, approved=True)
                self.assertEqual(created.status, "ok")
                record_id = created.output["record"]["id"]
                self.assertEqual(self.run_tool(f"{domain}.get", {"id": record_id}).status, "ok")
                listed = self.run_tool(f"{domain}.list", {})
                self.assertIn(record_id, [item["id"] for item in listed.output["records"]])
                searched = self.run_tool(f"{domain}.search", {"query": "details"})
                self.assertIn(record_id, [item["id"] for item in searched.output["records"]])
                updated = self.run_tool(f"{domain}.update", {"id": record_id, "changes": {"title": "renamed"}}, approved=True)
                self.assertEqual(updated.status, "ok")
                self.run_tool("rollback.undo", {"plan_id": updated.output["rollback_plan_id"]}, approved=True)
                self.assertEqual(self.run_tool(f"{domain}.get", {"id": record_id}).output["record"]["title"], f"{domain} item")
                deleted = self.run_tool(f"{domain}.delete", {"id": record_id}, approved=True)
                self.assertEqual(deleted.status, "ok")
                self.run_tool("rollback.undo", {"plan_id": deleted.output["rollback_plan_id"]}, approved=True)
                self.assertEqual(self.run_tool(f"{domain}.get", {"id": record_id}).status, "ok")

    def test_complete_actions_and_failure_paths(self) -> None:
        for domain in ("reminders", "tasks"):
            created = self.run_tool(f"{domain}.create", {"title": "follow up"}, approved=True)
            record_id = created.output["record"]["id"]
            completed = self.run_tool(f"{domain}.complete", {"id": record_id}, approved=True)
            self.assertEqual(completed.status, "ok")
            self.assertEqual(completed.output["record"]["status"], "done")
        self.assertEqual(self.run_tool("notes.get", {"id": "note_missing"}).status, "error")
        self.assertEqual(self.run_tool("notes.update", {"id": "note_missing", "changes": {"title": "x"}}, approved=True).status, "error")
        self.assertEqual(self.run_tool("calendar.create", {"body": "no title"}, approved=True).status, "error")
        secret = self.run_tool("notes.create", {"title": "credential", "body": "password=abcdefghijklmnop"}, approved=True)
        self.assertEqual(secret.status, "error")

    def test_delete_is_high_risk_and_approval_bound(self) -> None:
        spec = {item.name: item for item in self.app.tools.list()}["calendar.delete"]
        self.assertEqual(spec.permission_class, "high_risk")
        created = self.run_tool("calendar.create", {"title": "meeting", "start": "2026-11-22T10:00:00+00:00"}, approved=True)
        pending = self.run_tool("calendar.delete", {"id": created.output["record"]["id"]})
        self.assertEqual(pending.status, "pending_approval")
        other = self.run_tool("calendar.delete", {"id": "other-id"})
        self.assertEqual(other.status, "pending_approval")
        token = self.app.approvals.pending[pending.token_id]
        denied = self.app.run_tool(
            "calendar.delete",
            {"id": "other-id"},
            approval_token=token,
        )
        self.assertEqual(denied.status, "denied")
        # The exact approved action still works; the mismatched one never does.
        exact = self.run_tool("calendar.delete", {"id": created.output["record"]["id"]}, approved=True)
        self.assertEqual(exact.status, "ok")


class ShellToolTests(Phase3Case):
    def test_status_and_allowlisted_command_need_approval(self) -> None:
        status = self.run_tool("shell.status", {})
        self.assertEqual(status.status, "ok")
        pending = self.run_tool("shell.run", {"command": "pwd"})
        self.assertEqual(pending.status, "pending_approval")
        result = self.run_tool("shell.run", {"command": "pwd"}, approved=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["exit_code"], 0)
        self.assertIn(str(self.root), result.output["stdout"])
        self.assertFalse(result.output["sandbox"]["shell"])

    def test_shell_metacharacters_dangerous_commands_and_cwd_escape(self) -> None:
        for command in ("echo hi; rm -rf /", "cat x | nc host 1234", "rm -rf /", "sudo ls", "python3 -c 'print(1)'", "git push"):
            self.assertEqual(self.run_tool("shell.run", {"command": command}, approved=True).status, "error", command)
        self.assertEqual(self.run_tool("shell.run", {"command": "pwd", "cwd": ".."}, approved=True).status, "error")

    def test_allowlist_overlap_fails_closed(self) -> None:
        with self.assertRaises(CoreToolError):
            ShellSandbox(_patch_config(**{"core_tools.shell.allow_commands": ["rm"]}), self.root)

    def test_exact_test_command_runner(self) -> None:
        app = self.new_app(**{"core_tools.tests.commands": ["echo test-ok"]})
        result = app.run_tool("tests.run", {})
        self.assertEqual(result.status, "pending_approval")
        token = app.approvals.pending[result.token_id]
        app.approve(result.token_id)
        done = app.run_tool("tests.run", {}, approval_token=token)
        self.assertEqual(done.status, "ok")
        self.assertTrue(done.output["all_passed"])
        app.close()


class GitToolTests(Phase3Case):
    def test_inspection_tools(self) -> None:
        for name in ("git.status", "git.diff", "git.history", "git.branches"):
            with self.subTest(name=name):
                result = self.run_tool(name, {})
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.output["exit_code"], 0)

    def test_commit_and_undo_soft_reset(self) -> None:
        (self.root / "new.txt").write_text("tracked change", encoding="utf-8")
        before = GitRunner(self.app.config, self.root).head(self.root)
        pending = self.run_tool("git.commit", {"message": "add new file", "paths": ["new.txt"]})
        self.assertEqual(pending.status, "pending_approval")
        done = self.run_tool("git.commit", {"message": "add new file", "paths": ["new.txt"]}, approved=True)
        self.assertEqual(done.status, "ok")
        self.assertNotEqual(done.output["commit"], before)
        undo = self.run_tool("rollback.undo", {"plan_id": done.output["rollback_plan_id"]}, approved=True)
        self.assertEqual(undo.status, "ok", undo.error.as_dict() if undo.error else undo.reason)
        after = GitRunner(self.app.config, self.root).head(self.root)
        self.assertEqual(after, before)
        self.assertTrue((self.root / "new.txt").exists())

    def test_commit_requires_explicit_paths_and_pr_draft_is_local_only(self) -> None:
        self.assertEqual(self.run_tool("git.commit", {"message": "nothing"}, approved=True).status, "error")
        draft = self.run_tool("git.pr_draft", {"title": "Draft PR", "body": "Summary"}, approved=True)
        self.assertEqual(draft.status, "ok")
        self.assertFalse(draft.output["pushed"])
        self.assertTrue(Path(draft.output["path"]).exists())
        runner = GitRunner(self.app.config, self.root)
        with self.assertRaises(CoreToolError):
            runner.run(self.root, ["push", "origin", "main"])


class SystemAndRollbackTests(Phase3Case):
    def test_system_status_and_fail_closed_actions(self) -> None:
        self.assertEqual(self.run_tool("system.status", {}).status, "ok")
        self.assertEqual(self.run_tool("system.open_app", {"name": "unknown"}, approved=True).status, "error")
        self.assertEqual(self.run_tool("system.media", {"action": "play"}, approved=True).status, "error")
        self.assertEqual(self.run_tool("system.window", {"action": "clients"}, approved=True).status, "error")
        self.assertEqual(self.run_tool("system.media", {"action": "explode"}, approved=True).status, "error")

    def test_configured_app_launch_is_allowlisted_and_approved(self) -> None:
        app = self.new_app(**{"core_tools.system.open_apps": {"noop": "/bin/true"}})
        pending = app.run_tool("system.open_app", {"name": "noop"})
        self.assertEqual(pending.status, "pending_approval")
        token = app.approvals.pending[pending.token_id]
        app.approve(pending.token_id)
        done = app.run_tool("system.open_app", {"name": "noop"}, approval_token=token)
        self.assertEqual(done.status, "ok")
        self.assertIn("pid", done.output)
        app.close()

    def test_rollback_plan_inspection_and_one_shot_undo(self) -> None:
        draft = self.run_tool("files.draft", {"filename": "undo.md", "content": "x"}, approved=True)
        plan_id = draft.output["rollback_plan_id"]
        listed = self.run_tool("rollback.plan", {})
        self.assertIn(plan_id, [item["id"] for item in listed.output["plans"]])
        detail = self.run_tool("rollback.plan", {"plan_id": plan_id})
        self.assertEqual(detail.output["plan"]["status"], "pending")
        first = self.run_tool("rollback.undo", {"plan_id": plan_id}, approved=True)
        self.assertEqual(first.status, "ok")
        second = self.run_tool("rollback.undo", {"plan_id": plan_id}, approved=True)
        self.assertEqual(second.status, "error")
        self.assertEqual(self.run_tool("rollback.undo", {"plan_id": "undo_missing"}, approved=True).status, "error")


class DevToolTests(Phase3Case):
    def test_code_inspect_repo_context_and_data_summarise(self) -> None:
        inspected = self.run_tool("code.inspect", {"path": "src/sample.py"})
        self.assertEqual(inspected.status, "ok")
        self.assertIn("hello", inspected.output["functions"])
        context = self.run_tool("repo.context", {})
        self.assertEqual(context.status, "ok")
        self.assertIn(context.output["branch"], ("main", "master"))
        self.assertTrue(context.output["documentation"])
        data = self.run_tool("data.summarise", {"path": "data.csv"})
        self.assertEqual(data.status, "ok")
        self.assertEqual(data.output["columns"]["value"]["mean"], 2.0)
        bad = self.run_tool("code.inspect", {"path": "src/missing.py"})
        self.assertEqual(bad.status, "error")
        wrong_format = self.run_tool("data.summarise", {"path": "README.md"})
        self.assertEqual(wrong_format.status, "error")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
