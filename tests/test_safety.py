"""Safety, approval, redaction, audit and tool-registry tests."""

from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path

from omen.audit.log import AuditLog, detect_alerts
from omen.config import load_config
from omen.safety.approval import ActionLifecycle, ApprovalError, ApprovalGate, Stage
from omen.safety.kill_switch import KillSwitch
from omen.safety.policy import Decision, PolicyEngine
from omen.safety.redaction import redact_text, redact_value
from omen.tools.demo import DemoEcho, DemoNoteCreate
from omen.tools.registry import ToolRegistry, ToolSpec, ToolNotAllowedError, ToolNotFoundError

ROOT = Path(__file__).resolve().parent.parent


def base_config():
    cfg = copy.deepcopy(load_config(ROOT))
    cfg.sections["tools"]["retry"]["backoff_seconds"] = 0
    return cfg


class RedactionTests(unittest.TestCase):
    def test_api_keys_and_key_value_pairs(self):
        self.assertIn("[API_KEY_REDACTED]", redact_text("key is sk-TESTFAKEKEYONLY0000"))
        self.assertIn("[REDACTED]", redact_text("password=TESTFAKEPASSWORD here"))
        self.assertNotIn("TESTFAKEPASSWORD", redact_text("password=TESTFAKEPASSWORD here"))

    def test_hex_ids_survive_phone_redaction(self):
        token_id = "6702d57f188440059895c0f9e25350b4"
        self.assertEqual(redact_text(token_id), token_id)
        self.assertIn("[PHONE_REDACTED]", redact_text(f"token {token_id} call 555-123-4567"))
        self.assertIn(token_id, redact_text(f"token {token_id} call 555-123-4567"))

    def test_personal_data_and_recursive_redaction(self):
        text = "mail me at soham@example.com or +91 98765 43210"
        redacted = redact_text(text)
        self.assertNotIn("soham@example.com", redacted)
        self.assertNotIn("98765 43210", redacted)
        value = redact_value({"nested": {"api_key": "abc", "safe": "hello"}})
        self.assertEqual(value["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(value["nested"]["safe"], "hello")


class PolicyTests(unittest.TestCase):
    def test_read_only_allowed_and_unknown_blocked(self):
        policy = PolicyEngine(base_config())
        self.assertEqual(policy.decide({"tool": "demo.echo", "permission_class": "read_only"}), Decision.ALLOWED)
        self.assertEqual(policy.decide({"tool": "x", "permission_class": "magic"}), Decision.BLOCKED)

    def test_writes_need_approval_and_dry_run_is_allowed(self):
        policy = PolicyEngine(base_config())
        action = {"tool": "demo.note_create", "permission_class": "low_risk_write", "target": "local"}
        self.assertEqual(policy.decide(action), Decision.PENDING_APPROVAL)
        self.assertEqual(policy.decide({**action, "dry_run": True}), Decision.ALLOWED)

    def test_always_blocked_even_with_approval_claims(self):
        policy = PolicyEngine(base_config())
        outcome = policy.evaluate(
            {"tool": "demo.echo", "permission_class": "read_only", "target": "bank", "kind": "payment_execution", "approved": True}
        )
        self.assertEqual(outcome.decision, Decision.BLOCKED)
        self.assertEqual(outcome.alert, "blocked_action_attempt")

    def test_kill_switch_blocks_everything(self):
        switch = KillSwitch()
        switch.engage("test")
        policy = PolicyEngine(base_config(), kill_switch=switch)
        self.assertEqual(policy.decide({"tool": "demo.echo", "permission_class": "read_only"}), Decision.BLOCKED)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.gate = ApprovalGate(base_config(), clock=lambda: self.now[0])
        self.action = {"tool": "demo.note_create", "target": "local", "params": {"title": "a", "body": "b"}}

    def test_exact_binding_and_single_use(self):
        token = self.gate.request(self.action)
        self.assertFalse(self.gate.validate(token, self.action))
        self.gate.approve(token.token_id, "soham.bhagat")
        self.assertTrue(self.gate.validate(token, self.action))
        self.assertFalse(self.gate.validate(token, self.action))

    def test_parameter_change_requires_fresh_approval(self):
        token = self.gate.request(self.action)
        self.gate.approve(token.token_id, "soham.bhagat")
        changed = {**self.action, "params": {"title": "different", "body": "b"}}
        self.assertFalse(self.gate.validate(token, changed))
        self.assertTrue(self.gate.validate(token, self.action))

    def test_expiry(self):
        token = self.gate.request(self.action)
        self.gate.approve(token.token_id, "soham.bhagat")
        self.now[0] += 301
        self.assertFalse(self.gate.validate(token, self.action))
        self.assertEqual(token.state, "expired")

    def test_deny_and_unknown_token(self):
        token = self.gate.request(self.action)
        self.gate.deny(token.token_id, "soham.bhagat")
        self.assertFalse(self.gate.validate(token, self.action))
        with self.assertRaises(ApprovalError):
            self.gate.approve("does-not-exist", "soham.bhagat")

    def test_lifecycle_order_enforced(self):
        lifecycle = ActionLifecycle(self.action)
        with self.assertRaises(ApprovalError):
            lifecycle.advance(Stage.REPORT)
        for stage in (Stage.CONFIRM, Stage.EXECUTE, Stage.VERIFY, Stage.REPORT):
            lifecycle.advance(stage)
        self.assertTrue(lifecycle.complete())

    def test_kill_switch_cancels_pending(self):
        self.gate.request(self.action)
        self.assertEqual(self.gate.cancel_pending("test"), 1)
        self.assertEqual(self.gate.list_pending(), [])


class KillSwitchTests(unittest.TestCase):
    def test_engage_and_release(self):
        switch = KillSwitch()
        events = []
        switch.register(lambda reason: events.append(reason) or "ok")
        result = switch.engage("stop now")
        self.assertTrue(result["active"])
        self.assertEqual(events, ["stop now"])
        self.assertTrue(switch.status()["external_actions_locked"])
        switch.release(approver="soham.bhagat")
        self.assertFalse(switch.active)
        with self.assertRaises(ValueError):
            switch.release(approver="")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "logs" / "audit.jsonl"
        self.log = AuditLog(self.path, trace_id="test-trace")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_search_redaction_and_integrity(self):
        record = self.log.append(
            {
                "tool": "demo.echo",
                "target": "terminal",
                "args": {"api_key": "sk-TESTFAKEKEYONLY0000", "text": "mail soham@example.com"},
                "decision": "allowed",
                "status": "ok",
            }
        )
        self.assertEqual(record["trace_id"], "test-trace")
        self.assertEqual(record["args"]["api_key"], "[REDACTED]")
        self.assertNotIn("soham@example.com", record["args"]["text"])
        results = self.log.search(tool="demo.echo")
        self.assertEqual(len(results), 1)
        self.assertTrue(self.log.verify_integrity())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_tampering_detected(self):
        self.log.append({"tool": "demo.echo", "status": "ok"})
        content = self.path.read_text(encoding="utf-8").replace('"status":"ok"', '"status":"hacked"', 1)
        self.path.write_text(content, encoding="utf-8")
        self.assertFalse(self.log.verify_integrity())

    def test_append_only_and_alerts(self):
        before = self.path.exists()
        for _ in range(3):
            self.log.append({"tool": "demo.echo", "status": "error", "decision": "blocked"})
        self.assertTrue(before or self.path.exists())
        self.assertIn(
            "repeated_failures",
            {alert["type"] for alert in detect_alerts(self.log.search(), failure_threshold=3)},
        )


class SlowTool:
    name = "test.slow"
    description = "slow"
    permission_class = "read_only"

    def schema(self):
        return {"type": "object"}

    def dry_run(self, **kwargs):
        return {"would": "wait"}

    def execute(self, **kwargs):
        time.sleep(0.2)
        return {"done": True}


class FlakyTool:
    name = "test.flaky"
    description = "fails once"
    permission_class = "read_only"
    calls = 0

    def schema(self):
        return {"type": "object"}

    def dry_run(self, **kwargs):
        return {"would": "retry"}

    def execute(self, **kwargs):
        FlakyTool.calls += 1
        if FlakyTool.calls == 1:
            raise RuntimeError("first call fails")
        return {"done": True}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = base_config()
        cfg.sections["tools"]["retry"]["max_retries"] = 2
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.gate = ApprovalGate(cfg)
        self.registry = ToolRegistry(
            cfg,
            policy=PolicyEngine(cfg),
            approvals=self.gate,
            audit=self.audit,
            sleeper=lambda _seconds: None,
        )
        self.registry.register(DemoEcho())
        self.registry.register(DemoNoteCreate())

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_and_non_allowlisted_tools(self):
        with self.assertRaises(ToolNotFoundError):
            self.registry.execute("missing.tool", params={})
        self.registry.register(SlowTool(), spec=ToolSpec(
            name="test.slow", description="slow", permission_class="read_only", timeout_seconds=0.01, max_retries=0
        ))
        with self.assertRaises(ToolNotAllowedError):
            self.registry.execute("test.slow", params={})

    def test_timeout_is_structured_and_audited(self):
        self.registry.register(SlowTool(), spec=ToolSpec(
            name="test.slow", description="slow", permission_class="read_only", timeout_seconds=0.01, max_retries=0
        ))
        self.registry.allowlist = (*self.registry.allowlist, "test.slow")
        result = self.registry.execute("test.slow", params={})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, "timeout")
        self.assertEqual(self.audit.search(tool="test.slow", event_type="tool_call")[0]["status"], "error")

    def test_retry_policy(self):
        FlakyTool.calls = 0
        self.registry.register(FlakyTool())
        self.registry.allowlist = (*self.registry.allowlist, "test.flaky")
        result = self.registry.execute("test.flaky", params={})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.retries, 1)

    def test_approval_flow_single_use_and_binding(self):
        action = {"tool": "demo.note_create", "target": "local", "params": {"title": "t", "body": "b"}}
        pending = self.registry.execute("demo.note_create", params=action["params"], target="local")
        self.assertEqual(pending.status, "pending_approval")
        self.assertIn("approve", pending.approval_card["options"])
        token = self.gate.pending[pending.token_id]
        self.gate.approve(token.token_id, "soham.bhagat")
        changed = self.registry.execute(
            "demo.note_create", params={"title": "other", "body": "b"}, target="local", approval_token=token
        )
        self.assertEqual(changed.status, "denied")
        approved = self.registry.execute(
            "demo.note_create", params=action["params"], target="local", approval_token=token
        )
        self.assertEqual(approved.status, "ok")
        replay = self.registry.execute(
            "demo.note_create", params=action["params"], target="local", approval_token=token
        )
        self.assertEqual(replay.status, "denied")

    def test_dry_run_has_no_side_effects(self):
        result = self.registry.dry_run("demo.note_create", params={"title": "t", "body": "b"})
        self.assertEqual(result.status, "ok")
        self.assertIn("would_create_note", result.output)
        self.assertEqual(self.registry.get("demo.note_create").store.notes, {})


if __name__ == "__main__":
    unittest.main()
