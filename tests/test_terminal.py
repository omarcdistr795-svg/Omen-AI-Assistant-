"""Terminal chat, commands, progress, approval cards and end-to-end demo tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omen.app import OmenApp, parse_tool_calls
from omen.cli.commands import handle_command, parse_command
from omen.cli.render import render_approval_card, render_status
from omen.config import load_config
from scripts import benchmark, demo

ROOT = Path(__file__).resolve().parent.parent


class FakeAdapter:
    def __init__(self, name: str, text: str = "Hello there", *, local: bool = True):
        self.name = name
        self.model = "fake-model"
        self.local = local
        self.text = text
        self.calls = 0

    def complete(self, messages, *, stream=False, **kwargs):
        self.calls += 1
        if stream:
            return iter([self.text[:2], self.text[2:]])
        return {"content": self.text, "provider": self.name, "usage": {}}

    def health(self):
        return {"name": self.name, "configured": True, "local": self.local}


def make_app(root: Path, text: str = "Hello there") -> tuple[OmenApp, FakeAdapter]:
    cfg = copy.deepcopy(load_config(ROOT))
    cfg.sections["tools"]["retry"]["backoff_seconds"] = 0
    adapter = FakeAdapter("ollama", text)
    app = OmenApp(
        cfg,
        root=root,
        output=lambda _text: None,
        adapters={"ollama": adapter, "openai_compatible": FakeAdapter("cloud", local=False)},
    )
    return app, adapter


class CommandParsingTests(unittest.TestCase):
    def test_parse_command(self):
        self.assertIsNone(parse_command("plain text"))
        self.assertEqual(parse_command(" /status "), ("/status", ""))
        self.assertEqual(parse_command("/echo hello world"), ("/echo", "hello world"))

    def test_renderers_include_required_fields(self):
        card = render_approval_card({
            "token_id": "abc", "tool": "demo.note_create", "target": "local",
            "payload": {"api_key": "secret"}, "affected": ["local"], "risk": "low_risk_write",
            "expires_at": 1, "options": ["approve"], "plan": "Plan -> Confirm",
        })
        self.assertIn("APPROVAL CARD", card)
        self.assertNotIn("secret", card)
        card2 = render_approval_card({
            "token_id": "abc", "tool": "t", "target": "x", "payload": {}, "affected": [],
            "risk": "r", "expires_at": 1, "options": [], "plan": "p",
        })
        self.assertIn("token:    abc", card2)
        self.assertIn("risk:     r", card2)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.adapter = make_app(Path(self.tmp.name))

    def tearDown(self):
        if hasattr(self.app, "close"):
            self.app.close()
        self.tmp.cleanup()

    def test_streaming_returns_tokens_and_renders_progress(self):
        tokens = []
        progress = []
        answer = "".join(self.app.stream_chat("hi", on_token=tokens.append, on_progress=progress.append))
        self.assertEqual(answer, "Hello there")
        self.assertEqual(tokens, ["He", "llo there"])
        self.assertEqual(progress, [])
        self.assertGreaterEqual(self.adapter.calls, 1)

    def test_tool_block_is_parsed_and_routed_through_safety(self):
        self.assertEqual(
            parse_tool_calls('```tool\n{"name":"demo.echo","params":{"text":"x"}}\n```')[0]["name"],
            "demo.echo",
        )
        tool_text = '```tool\n{"name":"demo.note_create","target":"local","params":{"title":"t","body":"b"}}\n```'
        self.app, _ = make_app(Path(self.tmp.name), tool_text)
        progress = []
        self.app.chat("make a note", on_progress=progress.append)
        self.assertEqual(self.app.last_tool_results[0].status, "pending_approval")
        self.assertTrue(progress[0]["approval_card"]["token_id"])

    def test_malformed_tool_block_is_not_executed(self):
        self.app, _ = make_app(Path(self.tmp.name), '```tool\n{"name":123}\n```')
        self.app.chat("bad tool")
        self.assertEqual(self.app.last_tool_results, [])
        self.assertTrue(any(event.get("event_type") == "tool_parse_error" for event in self.app.history()))

    def test_status_and_health_include_resources_models_spend(self):
        status = self.app.status()
        rendered = render_status(status)
        for text in ("RAM:", "CPU:", "models:", "degrade:", "spend:"):
            self.assertIn(text, rendered)
        self.assertEqual(self.app.health()["status"], "ok")


class CommandDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, _ = make_app(Path(self.tmp.name))
        self.emitted: list[str] = []

    def tearDown(self):
        if hasattr(self.app, "close"):
            self.app.close()
        self.tmp.cleanup()

    def emit(self, text):
        self.emitted.append(text)

    def command(self, line):
        handled = handle_command(self.app, line, self.emit)
        self.assertTrue(handled)
        return "\n".join(self.emitted)

    def test_informational_commands(self):
        for line in ("/help", "/status", "/health", "/tools", "/brief", "/history status=ok limit=3"):
            with self.subTest(line=line):
                self.command(line)
        self.assertTrue(any("STATUS" in item for item in self.emitted))
        self.assertTrue(any("TOOLS" in item for item in self.emitted))
        self.assertTrue(any("HEALTH" in item for item in self.emitted))

    def test_plan_approve_deny_and_dry_run(self):
        self.command("/plan demo.note_create {\"title\":\"t\",\"body\":\"b\"}")
        self.assertIn("APPROVAL CARD", self.emitted[-1])
        token = next(iter(self.app.approvals.pending))
        self.command(f"/approve {token}")
        self.assertIn("approved", self.emitted[-1])
        self.command("/deny not-a-token")
        self.assertIn("Approval error", self.emitted[-1])
        self.emitted.clear()
        self.command("/dry-run demo.echo {\"text\":\"preview\"}")
        self.assertIn("would_echo", self.emitted[-1])

    def test_kill_blocks_further_actions(self):
        self.command("/kill terminal test")
        self.assertIn("KILL", self.emitted[-1])
        self.emitted.clear()
        self.command("/plan demo.echo {\"text\":\"x\"}")
        self.assertIn("blocked", self.emitted[-1].lower())

    def test_memory_stub_commands(self):
        self.command("/remember remember this")
        record_id = json.loads(self.emitted[-1].split("\n", 1)[1])["id"]
        self.emitted.clear()
        self.command(f"/forget {record_id}")
        self.assertIn('"removed": true', self.emitted[-1])

    def test_unknown_command_reports_itself(self):
        self.command("/warp")
        self.assertIn("Unknown command", self.emitted[-1])


class DemoAndBenchmarkTests(unittest.TestCase):
    def test_demo_shows_blocked_approved_and_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = make_app(Path(tmp))
            with patch("scripts.demo.OmenApp", return_value=app):
                events = demo.run()
            audit_events = [event for event in events if event.get("event_type") in ("tool_call", "approval", "kill_switch")]
            self.assertTrue(any(e["tool"] == "demo.note_create" and e["status"] == "blocked" for e in audit_events))
            self.assertTrue(any(e["tool"] == "demo.note_create" and e["status"] == "ok" for e in audit_events))
            self.assertTrue(any(e["tool"] == "demo.echo" and e["status"] == "blocked" for e in audit_events))
            self.assertTrue(any(e["event_type"] == "kill_switch" for e in audit_events))
            self.assertTrue(app.audit.verify_integrity())

    def test_benchmark_records_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = make_app(Path(tmp))
            output = Path(tmp) / "artifacts" / "benchmark.json"
            with patch("scripts.benchmark.OmenApp", return_value=app):
                report = benchmark.benchmark(iterations=2, output=output)
            self.assertEqual(report["latency_ms"]["iterations"], 2)
            self.assertIn("ram", report)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
