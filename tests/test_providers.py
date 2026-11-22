"""Provider adapters, fallback, cost control and resource-layer tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omen.audit.log import AuditLog
from omen.config import load_config
from omen.cost.budget import BudgetController
from omen.providers.base import AdapterError
from omen.providers.factory import ProviderRouter
from omen.providers.ollama import OllamaAdapter
from omen.providers.openai_compatible import OpenAICompatibleAdapter
from omen.resources.degradation import DegradationPolicy
from omen.resources.lifecycle import ModelBudgetError, ModelLifecycleManager
from omen.resources.monitor import ResourceMonitor

ROOT = Path(__file__).resolve().parent.parent


def base_config():
    return copy.deepcopy(load_config(ROOT))


class FakeAdapter:
    def __init__(self, name: str, *, local: bool = True, text: str = "ok", fail: bool = False):
        self.name = name
        self.local = local
        self.model = "fake-model"
        self.text = text
        self.fail = fail
        self.calls = 0

    def complete(self, messages, *, stream=False, **kwargs):
        self.calls += 1
        if self.fail:
            raise AdapterError("fake failure", provider=self.name)
        if stream:
            return iter([self.text[:2], self.text[2:]])
        return {"content": self.text, "provider": self.name, "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    def health(self):
        return {"name": self.name, "configured": True, "local": self.local}


class OpenAIAdapterTests(unittest.TestCase):
    def test_complete_and_streaming_with_mocked_http(self):
        adapter = OpenAICompatibleAdapter("cloud", {
            "base_url": "https://example.test/v1", "model": "m", "api_key": "sk-TESTFAKEKEYONLY0000", "timeout_seconds": 1
        })
        with patch("omen.providers.openai_compatible.post_json", return_value={
            "choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}
        }) as post:
            result = adapter.complete([{"role": "user", "content": "hi"}])
            self.assertEqual(result["content"], "hello")
            self.assertIn("Bearer sk-TESTFAKEKEYONLY0000", post.call_args.kwargs["headers"]["Authorization"])
        chunks = ['{"choices":[{"delta":{"content":"he"}}]}', '{"choices":[{"delta":{"content":"llo"}}]}']
        with patch("omen.providers.openai_compatible.post_stream", return_value=iter(chunks)):
            streamed = "".join(adapter.complete([{"role": "user", "content": "hi"}], stream=True))
            self.assertEqual(streamed, "hello")
        self.assertTrue(adapter.health()["api_key_present"])
        self.assertNotIn("sk-TESTFAKEKEYONLY0000", str(adapter.health()))


class OllamaAdapterTests(unittest.TestCase):
    def test_complete_and_streaming_with_mocked_http(self):
        adapter = OllamaAdapter("local", {"base_url": "http://127.0.0.1:11434", "model": "m"})
        with patch("omen.providers.ollama.post_json", return_value={
            "message": {"content": "local answer"}, "prompt_eval_count": 4, "eval_count": 3
        }):
            result = adapter.complete([{"role": "user", "content": "hi"}])
            self.assertEqual(result["content"], "local answer")
            self.assertEqual(result["usage"]["eval_count"], 3)
        lines = [
            json.dumps({"message": {"content": "lo"}}),
            json.dumps({"message": {"content": "cal"}, "done": True}),
        ]
        with patch("omen.providers.ollama.post_lines", return_value=iter(lines)):
            self.assertEqual("".join(adapter.complete([], stream=True)), "local")
        self.assertTrue(adapter.health()["local"])


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.cfg = base_config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fallback_on_provider_error(self):
        self.cfg.sections["providers"]["default"] = "primary"
        self.cfg.sections["providers"]["fallback"] = ["backup"]
        adapters = {"primary": FakeAdapter("primary", fail=True), "backup": FakeAdapter("backup", text="fallback ok")}
        router = ProviderRouter(self.cfg, adapters=adapters, audit=self.audit)
        result = router.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "fallback ok")
        self.assertEqual(len(self.audit.search(event_type="provider_fallback")), 1)

    def test_streaming_fallback_before_first_token(self):
        self.cfg.sections["providers"]["default"] = "primary"
        self.cfg.sections["providers"]["fallback"] = ["backup"]
        adapters = {"primary": FakeAdapter("primary", fail=True), "backup": FakeAdapter("backup", text="stream ok")}
        router = ProviderRouter(self.cfg, adapters=adapters)
        self.assertEqual("".join(router.complete([], stream=True)), "stream ok")

    def test_budget_blocks_cloud_and_uses_local(self):
        cfg = base_config()
        budget = BudgetController(cfg, state_path=Path(self.tmp.name) / "cost.json", persist=False)
        notices = []
        router = ProviderRouter(
            cfg,
            adapters={"ollama": FakeAdapter("ollama"), "openai_compatible": FakeAdapter("cloud", local=False, fail=True)},
            budget=budget,
            audit=self.audit,
            notice=notices.append,
        )
        result = router.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "ok")
        self.assertTrue(any("cloud disabled" in notice for notice in notices))
        self.assertTrue(self.audit.search(event_type="cost_usage"))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _controller(self, budget_usd=10.0):
        cfg = base_config()
        cfg.sections["cost"].update({"enabled": True, "monthly_budget_usd": budget_usd, "warn_at_percent": 80})
        return BudgetController(cfg, state_path=Path(self.tmp.name) / "cost.json")

    def test_warning_hard_stop_and_local_fallback(self):
        controller = self._controller()
        self.assertTrue(controller.authorize(provider="cloud", local=False).allowed)
        warning = controller.record(provider="cloud", local=False, tokens_in=100, tokens_out=50, cost_usd=8.5)
        self.assertEqual(warning["type"], "budget_warning")
        decision = controller.authorize(provider="cloud", local=False)
        self.assertTrue(decision.warning)
        stop = controller.record(provider="cloud", local=False, cost_usd=1.5)
        self.assertEqual(stop["type"], "budget_hard_stop")
        blocked = controller.authorize(provider="cloud", local=False)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.fallback, "ollama")
        self.assertTrue(controller.authorize(provider="ollama", local=True).allowed)
        self.assertEqual(controller.status()["percent_used"], 100.0)

    def test_per_request_token_cap(self):
        controller = self._controller()
        decision = controller.authorize(provider="cloud", local=False, estimated_tokens=5000)
        self.assertFalse(decision.allowed)
        self.assertIn("token cap", decision.reason)

    def test_zero_budget_default_blocks_cloud(self):
        cfg = base_config()
        controller = BudgetController(cfg, state_path=Path(self.tmp.name) / "zero.json")
        decision = controller.authorize(provider="cloud", local=False)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.fallback, "ollama")


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = base_config()

    def _monitor(self, percent):
        return ResourceMonitor(self.cfg, sampler=lambda: {
            "ram_total_gb": 16.0,
            "ram_available_gb": 16.0 * (1 - percent / 100),
            "cpu_percent": 0.0,
            "load1": 0.0,
        })

    def test_warn_degrade_emergency_thresholds(self):
        self.assertEqual(self._monitor(79).level(), "normal")
        self.assertEqual(self._monitor(80).level(), "warn")
        self.assertEqual(self._monitor(85).level(), "degrade")
        self.assertEqual(self._monitor(92).level(), "emergency")
        snap = self._monitor(85).snapshot()
        self.assertEqual(snap.ram_total_gb, 16.0)

    def test_degradation_order_and_protected_components(self):
        policy = DegradationPolicy(self.cfg)
        degrade = policy.apply("degrade", active=list(policy.order), loaded_models=["local_reasoning"])
        self.assertEqual([item["component"] for item in degrade], ["dashboard", "heavy_scheduled_jobs"])
        emergency = policy.apply("emergency", active=[*policy.order, "audit_log", "kill_switch"], loaded_models=["local_reasoning"])
        names = [item["component"] for item in emergency]
        self.assertEqual(names, policy.order)
        self.assertNotIn("audit_log", names)
        self.assertNotIn("kill_switch", names)
        reasoning = next(item for item in emergency if item["component"] == "local_reasoning_model")
        self.assertIn("unload", reasoning["action"])

    def test_model_lifecycle_lazy_load_one_heavy_idle_unload(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.sections["resources"]["models"]["second_heavy"] = {"kind": "reasoning", "memory_gb": 4.0, "heavy": True}
        manager = ModelLifecycleManager(cfg, clock=lambda: 0.0)
        manager.load("local_reasoning")
        manager.load("embeddings")
        self.assertEqual(manager.loaded(), ["embeddings", "local_reasoning"])
        manager.load("second_heavy")
        self.assertNotIn("local_reasoning", manager.loaded())
        self.assertEqual(len(manager.status()["heavy_loaded"]), 1)
        manager.touch("embeddings", now=0.0)
        self.assertIn("embeddings", manager.unload_idle(now=400))
        self.assertEqual(manager.loaded(), [])
        cfg.sections["resources"]["models"]["huge"] = {"kind": "test", "memory_gb": 12.0, "heavy": False}
        with self.assertRaises(ModelBudgetError):
            ModelLifecycleManager(cfg).load("huge")


if __name__ == "__main__":
    unittest.main()
