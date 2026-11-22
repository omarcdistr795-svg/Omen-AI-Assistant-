"""Phase 6 dashboard tests: auth, approval gating, no secrets, local-only, degradation."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from omen.app import OmenApp
from omen.config import load_config
from omen.dashboard import DashboardServer
from omen.tools.demo import DemoNoteCreate

ROOT = Path(__file__).resolve().parent.parent


class FakeAdapter:
    def __init__(self, name: str = "ollama", text: str = "ok", local: bool = True):
        self.name = name
        self.model = "fake-model"
        self.local = local
        self.text = text

    def complete(self, messages, *, stream=False, **kwargs):
        if stream:
            return iter([self.text])
        return {"content": self.text, "provider": self.name, "usage": {}}

    def health(self):
        return {"name": self.name, "configured": True, "local": self.local}


def make_cfg(tmp: Path, **dashboard):
    cfg = load_config(ROOT)
    data = json.loads(json.dumps(cfg.sections))
    data["dashboard"].update({
        "enabled": True,
        "host": "127.0.0.1",
        "port": 0,
        "auth_enabled": True,
        "token_env": "OMEN_DASHBOARD_TOKEN",
        "allow_remote": False,
        "remote_read_only": True,
        "tls_cert": None,
        "tls_key": None,
        "push_notifications": False,
    })
    data["dashboard"].update(dashboard)
    data["system"]["state_dir"] = str(tmp / "state")
    from omen.config import Config
    c = Config(sections=data, source=cfg.source)
    return c


def http_json(url, token=None, method="GET", body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".env").write_text("OMEN_DASHBOARD_TOKEN=test-dashboard-token\n", encoding="utf-8")
        cfg = make_cfg(self.root)
        self.app = OmenApp(cfg, root=self.root, output=lambda _t: None, adapters={"ollama": FakeAdapter(), "openai_compatible": FakeAdapter("cloud", local=False)})
        self.app.tools.register(DemoNoteCreate())
        self.server = DashboardServer(self.app)
        self.server.start()
        # bind to actual port chosen by OS
        self.base = f"http://127.0.0.1:{self.server._server.server_address[1]}"

    def tearDown(self):
        try:
            self.server.close()
        finally:
            self.app.close()
            self.tmp.cleanup()

    def test_auth_required_and_token_works(self):
        with self.assertRaises(HTTPError) as ctx:
            http_json(f"{self.base}/api/status")
        self.assertEqual(ctx.exception.code, 401)
        status, data = http_json(f"{self.base}/api/status", token="test-dashboard-token")
        self.assertEqual(status, 200)
        self.assertIn("resources", data)
        self.assertIn("cost", data)

    def test_approvals_go_through_app_gate(self):
        # create pending approval via public app interface
        result = self.app.run_tool("demo.note_create", {"title": "x", "body": "y"})
        self.assertEqual(result.status, "pending_approval")
        token_id = result.token_id
        status, data = http_json(f"{self.base}/api/approvals", token="test-dashboard-token")
        self.assertEqual(status, 200)
        ids = [a["token_id"] for a in data["approvals"]]
        self.assertIn(token_id, ids)
        # deny through dashboard endpoint - must call app.deny
        status, data = http_json(f"{self.base}/api/approvals/{token_id}/deny", token="test-dashboard-token", method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["state"], "denied")
        # token is no longer pending
        status, data = http_json(f"{self.base}/api/approvals", token="test-dashboard-token")
        self.assertNotIn(token_id, [a["token_id"] for a in data["approvals"]])

    def test_approve_through_dashboard_executes_via_app(self):
        result = self.app.run_tool("demo.note_create", {"title": "x", "body": "y"})
        token_id = result.token_id
        status, data = http_json(f"{self.base}/api/approvals/{token_id}/approve", token="test-dashboard-token", method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["state"], "approved")
        # execute with approved token through normal app flow
        token = self.app.approvals.pending.get(token_id)
        self.assertIsNotNone(token)
        executed = self.app.run_tool("demo.note_create", {"title": "x", "body": "y"}, approval_token=token)
        self.assertEqual(executed.status, "ok")

    def test_no_secrets_exposed(self):
        secret = "sk-THISSHOULDNOTLEAK1234567890"
        self.app.audit.append({"event_type": "test", "tool": "test", "status": "ok", "summary": f"used {secret}", "args": {"api_key": secret}})
        status, data = http_json(f"{self.base}/api/status", token="test-dashboard-token")
        blob = json.dumps(data)
        self.assertNotIn(secret, blob)
        self.assertNotIn("test-dashboard-token", blob)

    def test_memory_browser_uses_public_interface(self):
        if hasattr(self.app.memory, "remember"):
            self.app.memory.remember("dashboard remembers this", kind="note")
        status, data = http_json(f"{self.base}/api/memory/search?q=dashboard", token="test-dashboard-token")
        self.assertEqual(status, 200)
        self.assertIn("items", data)

    def test_local_only_default_and_remote_requires_tls(self):
        cfg = make_cfg(self.root, host="0.0.0.0", allow_remote=True, tls_cert=None, tls_key=None)
        with self.assertRaises(Exception):
            DashboardServer(self.app, config=cfg.sections["dashboard"])

    def test_remote_read_only_blocks_approval_actions(self):
        # simulate remote read-only by flipping flag on running server
        self.server.allow_remote = True
        self.server.remote_read_only = True
        result = self.app.run_tool("demo.note_create", {"title": "x", "body": "y"})
        with self.assertRaises(HTTPError) as ctx:
            http_json(f"{self.base}/api/approvals/{result.token_id}/deny", token="test-dashboard-token", method="POST")
        self.assertEqual(ctx.exception.code, 403)

    def test_degradation_stops_dashboard(self):
        # dashboard is the first thing to shed under memory pressure
        self.app.dashboard = self.server
        self.app.monitor.level = lambda snapshot=None: "emergency"  # type: ignore
        snap = type("S", (), {"ram_percent": 95.0, "cpu_percent": 10.0, "ram_used_gb": 8.0, "ram_total_gb": 16.0, "ram_available_gb": 8.0, "load1": 1.0, "as_dict": lambda self: {"ram_percent": 95.0, "cpu_percent": 10.0}})()
        self.app.monitor.snapshot = lambda: snap  # type: ignore
        self.app.monitor.status = lambda: {"ram_percent": 95.0, "cpu_percent": 10.0, "ram_used_gb": 8.0, "ram_total_gb": 16.0, "level": "emergency", "ram_available_gb": 8.0, "load1": 1.0}  # type: ignore
        self.app.degradation_tick()
        self.assertIsNone(self.app.dashboard)
        self.assertTrue(self.app._dashboard_disabled_by_pressure)


class DashboardConfigTests(unittest.TestCase):
    def test_dashboard_section_defaults(self):
        cfg = load_config(ROOT)
        self.assertIn("dashboard", cfg.sections)
        self.assertEqual(cfg.get("dashboard.host"), "127.0.0.1")
        self.assertTrue(cfg.get("dashboard.auth_enabled"))

if __name__ == "__main__":
    unittest.main()
