"""Core scaffold, config, prompt, interfaces and stub tests."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from omen import interfaces, prompt
from omen.config import ConfigError, load_config
from omen.stubs.event_bus import InMemoryEventBus
from omen.stubs.memory import InMemoryMemoryStore
from omen.stubs.notifier import NoOpNotifier
from omen.stubs.scheduler import NoOpScheduler
from omen.stubs.voice import NoOpVoiceIO

ROOT = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    def test_defaults_load_and_validate(self):
        cfg = load_config(ROOT)
        for section in ("system", "providers", "tools", "safety", "resources", "cost"):
            self.assertIn(section, cfg.sections)
        self.assertEqual(cfg.get("providers.default"), "ollama")
        self.assertEqual(cfg.get("cost.monthly_budget_usd"), 0.0)

    def test_layered_config_and_env_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(ROOT / "config.yaml", root / "config.yaml")
            (root / "config.local.yaml").write_text(
                "system:\n  name: Omen-Local\nresources:\n  idle_unload_seconds: 42\n", encoding="utf-8"
            )
            (root / ".env").write_text("OMEN_OPENAI_API_KEY=sk-TESTFAKEKEYONLY0000\n", encoding="utf-8")
            cfg = load_config(root)
            self.assertEqual(cfg.get("system.name"), "Omen-Local")
            self.assertEqual(cfg.get("resources.idle_unload_seconds"), 42)
            self.assertEqual(cfg.get("providers.adapters.openai_compatible.api_key"), "sk-TESTFAKEKEYONLY0000")
            self.assertNotIn("sk-TESTFAKEKEYONLY0000", repr(cfg))
            self.assertIn("***REDACTED***", repr(cfg.redacted()))

    def test_missing_env_secret_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = (ROOT / "config.yaml").read_text(encoding="utf-8").replace(
                "api_key_env: OMEN_OPENAI_API_KEY", "api_key_env: OMEN_MISSING_KEY"
            )
            (root / "config.yaml").write_text(content, encoding="utf-8")
            (root / ".env").write_text("OMEN_UNRELATED=1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_bad_thresholds_rejected(self):
        cfg = load_config(ROOT)
        cfg.sections["resources"]["degrade_percent"] = 70
        with self.assertRaises(ConfigError):
            from omen.config import validate_config

            validate_config(cfg)

    def test_env_example_and_gitignore_are_safe(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("OMEN_OPENAI_API_KEY=", env_example)
        self.assertIn(".env", gitignore)
        self.assertIn("config.local.yaml", gitignore)
        self.assertNotIn("sk-", env_example)


class PromptAndInterfaceTests(unittest.TestCase):
    def test_prompt_contains_personality_and_no_secrets(self):
        for phrase in ("calm", "Never fabricate", "Local-first", "Soham Bhagat"):
            self.assertIn(phrase, prompt.SYSTEM_PROMPT)
        self.assertNotIn("api_key", prompt.SYSTEM_PROMPT.lower())

    def test_protocols_are_documented(self):
        for protocol in (
            interfaces.ProviderAdapter,
            interfaces.Tool,
            interfaces.MemoryStore,
            interfaces.Scheduler,
            interfaces.VoiceIO,
            interfaces.EventBus,
            interfaces.Notifier,
        ):
            self.assertTrue(hasattr(protocol, "__protocol_attrs__") or hasattr(protocol, "_is_protocol"))


class StubTests(unittest.TestCase):
    def test_memory_stub(self):
        store = InMemoryMemoryStore()
        record = store.remember("secret note", kind="note")
        self.assertEqual(len(store.recall("secret")), 1)
        self.assertTrue(store.forget(record["id"]))
        self.assertEqual(store.recall("secret"), [])

    def test_scheduler_stub(self):
        scheduler = NoOpScheduler()
        job = scheduler.schedule("brief", "09:00", {"kind": "daily"})
        self.assertEqual(job["status"], "stubbed")
        self.assertEqual(len(scheduler.list_jobs()), 1)
        self.assertTrue(scheduler.cancel(job["id"]))

    def test_voice_stub_never_speaks(self):
        voice = NoOpVoiceIO()
        result = voice.speak("hello")
        self.assertFalse(result["spoken"])
        self.assertEqual(voice.listen()["heard"], None)
        self.assertEqual(voice.stop()["stopped"], 1)

    def test_event_bus_stub(self):
        bus = InMemoryEventBus()
        received = []
        bus.subscribe("test", received.append)
        self.assertEqual(bus.publish("test", {"value": 1}), 1)
        self.assertEqual(received[0]["value"], 1)

    def test_notifier_stub_sends_nothing(self):
        notifier = NoOpNotifier()
        self.assertFalse(notifier.notify("ping")["delivered"])
        self.assertEqual(len(notifier.notifications), 1)


if __name__ == "__main__":
    unittest.main()
