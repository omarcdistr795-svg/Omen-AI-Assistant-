"""Phase 7 release acceptance and hardening checks; all external services mocked."""

from __future__ import annotations

import array
import copy
import io
import json
import os
import resource
import tarfile
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from omen.app import OmenApp
from omen.audit.log import AuditError, AuditLog
from omen.config import load_config
from omen.memory import OmenMemoryStore
from omen.providers.base import AdapterError
from omen.voice.audio import MockAudioDevice
from omen.voice.pipeline import Transcription

ROOT = Path(__file__).resolve().parent.parent


class FakeAdapter:
    def __init__(self, name: str, text: str = "Ready.", *, local: bool = True, fail: bool = False):
        self.name = name
        self.model = f"{name}-test-model"
        self.local = local
        self.text = text
        self.fail = fail
        self.messages: list[dict] = []

    def complete(self, messages, *, stream=False, **_kwargs):
        self.messages = messages
        if self.fail:
            raise AdapterError("mock provider unavailable", provider=self.name)
        if stream:
            return iter([self.text])
        return {"content": self.text, "provider": self.name, "usage": {}}

    def health(self):
        return {"name": self.name, "configured": True, "local": self.local}


class FakeSynthesizer:
    def __init__(self):
        self.spoken: list[str] = []

    def synthesize(self, text: str) -> Path:
        self.spoken.append(text)
        handle = tempfile.NamedTemporaryFile(prefix="omen-phase7-", suffix=".wav", delete=False)
        handle.write(b"mock audio")
        handle.close()
        return Path(handle.name)


class FakeRecognizer:
    def __init__(self, transcript: str = "status update"):
        self.result = Transcription(transcript, 0.99)

    def transcribe(self, _sample):
        return self.result


class FakeVAD:
    def analyze(self, _sample):
        return SimpleNamespace(voiced=True, duration_seconds=0.5, voiced_ratio=0.9)


class TestPhase7Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="omen-phase7-")
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(load_config(ROOT))
        self.cfg.sections["tools"]["retry"]["backoff_seconds"] = 0
        self.cfg.sections["voice"]["follow_up_enabled"] = False
        self.app: OmenApp | None = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.tmp.cleanup()

    def make_app(self, *, reply="Ready.", adapters=None) -> OmenApp:
        if adapters is None:
            adapters = {
                "ollama": FakeAdapter("ollama", reply),
                "openai_compatible": FakeAdapter("openai_compatible", "Cloud reply", local=False),
            }
        self.app = OmenApp(self.cfg, root=self.root, output=lambda _text: None, adapters=adapters)
        return self.app

    def configure_voice_mock(self, app: OmenApp, transcript: str, *, block_playback=False):
        app.voice.audio = MockAudioDevice(block_playback=block_playback)
        app.voice.recognizer = FakeRecognizer(transcript)
        app.voice.vad = FakeVAD()
        app.voice.synthesizer = FakeSynthesizer()
        app.voice.confirmation = lambda _text, _confidence: True

    def test_acceptance_1_voice_reminder_transcript_creation_and_spoken_alert(self):
        self.cfg.sections["automation"]["notifications"]["channels"]["voice"] = True
        due = datetime.now(timezone.utc) + timedelta(minutes=10)
        params = {
            "name": "push the repo",
            "when": due.isoformat(),
            "payload": {
                "kind": "notification",
                "message": "Reminder: push the repo",
                "priority": "critical",
                "channel": "voice",
            },
        }
        reply = "I will prepare that reminder for approval.\n```tool\n" + json.dumps(
            {"name": "automation.schedule", "params": params}
        ) + "\n```"
        app = self.make_app(reply=reply)
        self.configure_voice_mock(app, "remind me in 10 minutes to push the repo")
        result = app.voice.process_sample(self.root / "mock-recording.wav")

        self.assertTrue(result["accepted"])
        self.assertTrue(any("Heard: remind me in 10 minutes to push the repo" in line for line in app.notices))
        pending = app.last_tool_results[0]
        self.assertEqual(pending.status, "pending_approval")
        token = app.approvals.pending[pending.token_id]
        app.approve(pending.token_id)
        created = app.run_tool("automation.schedule", params, approval_token=token)
        self.assertEqual(created.status, "ok")
        self.assertEqual(created.output["name"], "push the repo")
        self.assertEqual(created.output["next_run"], due.isoformat())
        app.scheduler.tick(now=due + timedelta(seconds=1))
        spoken = app.automation.notifier.notifications[-1]
        self.assertEqual(spoken["status"], "delivered")
        self.assertIn("Reminder: push the repo", app.voice.synthesizer.spoken)

    def test_acceptance_2_risky_delete_approval_audit_and_changed_path(self):
        app = self.make_app()
        folder = self.root / "temporary-folder"
        folder.mkdir()
        nested = folder / "nested"
        nested.mkdir()
        target = nested / "note.txt"
        target.write_text("temporary", encoding="utf-8")
        pending = app.run_tool("files.delete", {"path": str(folder)})
        card = pending.approval_card
        self.assertEqual(card["payload"]["path"], str(folder))
        self.assertEqual(card["risk"], "high_risk")
        token = app.approvals.pending[pending.token_id]
        app.approve(pending.token_id)
        done = app.run_tool("files.delete", {"path": str(folder)}, approval_token=token)
        self.assertEqual(done.status, "ok")
        self.assertFalse(folder.exists())
        quarantined = Path(done.output["quarantine"])
        self.assertTrue((quarantined / "nested" / "note.txt").is_file())
        self.assertEqual((quarantined / "nested" / "note.txt").read_text(encoding="utf-8"), "temporary")

        changed = self.root / "different-folder.txt"
        changed.write_text("temporary", encoding="utf-8")
        changed_result = app.run_tool("files.delete", {"path": str(changed)})
        self.assertEqual(changed_result.status, "pending_approval")
        events = app.audit.search()
        folder_events = [event for event in events if event.get("args", {}).get("path") == str(folder)]
        stages = {event.get("status") for event in folder_events if event.get("event_type") == "tool_lifecycle"}
        approval_logged = any(
            event.get("event_type") == "approval"
            and (event.get("approval", {}).get("exact_action", {}).get("params", {}).get("path") == str(folder))
            for event in events
        )
        self.assertTrue(approval_logged)
        self.assertIn("plan", stages)
        self.assertIn("execute", stages)
        self.assertIn("verify", stages)

        undo_pending = app.run_tool("rollback.undo", {"plan_id": done.output["rollback_plan_id"]})
        self.assertEqual(undo_pending.status, "pending_approval")
        undo_token = app.approvals.pending[undo_pending.token_id]
        app.approve(undo_pending.token_id)
        restored = app.run_tool(
            "rollback.undo", {"plan_id": done.output["rollback_plan_id"]}, approval_token=undo_token
        )
        self.assertEqual(restored.status, "ok")
        self.assertEqual((folder / "nested" / "note.txt").read_text(encoding="utf-8"), "temporary")
        self.assertTrue(app.audit.verify_integrity())

    def test_directory_delete_rejects_symlink_escapes_and_wildcards(self):
        app = self.make_app()
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("outside", encoding="utf-8")
        link = self.root / "escape"
        link.symlink_to(outside, target_is_directory=True)
        pending = app.run_tool("files.delete", {"path": str(link)})
        token = app.approvals.pending[pending.token_id]
        app.approve(pending.token_id)
        denied = app.run_tool("files.delete", {"path": str(link)}, approval_token=token)
        self.assertEqual(denied.status, "error")
        self.assertTrue((outside / "keep.txt").exists())

        wildcard = app.run_tool("files.delete", {"path": str(self.root / "*")})
        self.assertEqual(wildcard.status, "pending_approval")
        wildcard_token = app.approvals.pending[wildcard.token_id]
        app.approve(wildcard.token_id)
        wildcard_result = app.run_tool(
            "files.delete", {"path": str(self.root / "*")}, approval_token=wildcard_token
        )
        self.assertEqual(wildcard_result.status, "error")
        self.assertTrue((outside / "keep.txt").exists())

    def test_acceptance_3_preference_survives_restart_and_forget_removes_export(self):
        first = self.make_app()
        first.memory.set_preference("verbosity", "concise")
        first.memory.close()
        first.close()
        self.app = None

        second = self.make_app(reply="I will keep replies concise.")
        self.assertEqual(second.memory.get_preference("verbosity"), "concise")
        second.chat("Preference verbosity concise")
        self.assertIn("concise", second.router.adapters["ollama"].messages[0]["content"])
        item = next(item for item in second.memory.recall("", kind="preference", limit=50)
                    if item.get("metadata", {}).get("pref_key") == "verbosity")
        self.assertTrue(second.memory.forget(item["id"]))
        exported = json.dumps(second.memory.export_data(), sort_keys=True)
        self.assertNotIn("concise", exported)
        self.assertNotIn("verbosity", second.memory.list_preferences())

    def test_acceptance_4_pressure_degradation_and_clean_recovery(self):
        app = self.make_app()
        pressure = {"level": "normal"}
        app.monitor.level = lambda: pressure["level"]
        app.monitor.status = lambda: {
            "ram_total_gb": 16.0, "ram_available_gb": 0.8, "ram_used_gb": 15.2,
            "ram_percent": 95.0 if pressure["level"] == "emergency" else 86.0,
            "cpu_percent": 10.0, "load1": 0.1, "level": pressure["level"],
        }

        class DashboardMock:
            stopped = False

            def stop(self):
                self.stopped = True

            def status(self):
                return {"running": not self.stopped}

        dashboard = DashboardMock()
        app.dashboard = dashboard
        app.models.load("local_reasoning")
        app.memory.remember("Phase 7 indexing soak item", wait_for_index=False)
        pressure["level"] = "degrade"
        first = app.degradation_tick()
        self.assertTrue(dashboard.stopped)
        self.assertIn("dashboard", [action["component"] for action in first])
        self.assertIn("heavy_scheduled_jobs", [action["component"] for action in first])

        pressure["level"] = "emergency"
        emergency = app.degradation_tick()
        components = [action["component"] for action in emergency]
        self.assertIn("memory_reindexing", components)
        self.assertIn("local_reasoning_model", components)
        self.assertTrue(app.memory.is_indexing_paused())
        self.assertNotIn("local_reasoning", app.models.loaded())
        self.assertEqual(app.voice.status()["degradation"], "text_only")

        pressure["level"] = "normal"
        app.degradation_tick()
        self.assertFalse(app.memory.is_indexing_paused())
        self.assertEqual(app.voice.status()["degradation"], "normal")
        self.assertFalse(app._dashboard_disabled_by_pressure)
        self.assertTrue(any("recovered" in note.lower() for note in app.notices))

    def test_acceptance_5_kill_switch_stops_speech_cancels_pending_and_locks_tools(self):
        app = self.make_app()
        self.configure_voice_mock(app, "status update", block_playback=True)
        speaker = threading.Thread(target=lambda: app.voice.speak("A long spoken response."), daemon=True)
        speaker.start()
        self.assertTrue(app.voice.audio.playing.wait(timeout=1))

        folder = self.root / "pending-delete"
        folder.mkdir()
        target = folder / "note.txt"
        target.write_text("temporary", encoding="utf-8")
        pending = app.run_tool("files.delete", {"path": str(target)})
        token = app.approvals.pending[pending.token_id]
        app.kill("Phase 7 mid-speech test")
        speaker.join(timeout=1)
        self.assertFalse(speaker.is_alive())
        self.assertEqual(token.state, "cancelled")
        blocked = app.run_tool("files.delete", {"path": str(target)})
        self.assertEqual(blocked.status, "blocked")
        self.assertTrue(app.audit.search(event_type="kill_switch"))

    def test_acceptance_6_cloud_budget_exhaustion_falls_back_with_notice(self):
        self.cfg.sections["providers"]["default"] = "openai_compatible"
        self.cfg.sections["providers"]["fallback"] = ["ollama"]
        self.cfg.sections["cost"].update({"enabled": True, "monthly_budget_usd": 1.0, "local_fallback": "ollama"})
        adapters = {
            "openai_compatible": FakeAdapter("openai_compatible", "cloud", local=False),
            "ollama": FakeAdapter("ollama", "local fallback answer"),
        }
        app = self.make_app(adapters=adapters)
        app.budget.record(provider="openai_compatible", local=False, cost_usd=1.0)
        result = app.router.complete([{"role": "user", "content": "Give a short status."}])
        self.assertEqual(result["content"], "local fallback answer")
        self.assertTrue(any("monthly budget exhausted" in note for note in app.notices))
        self.assertTrue(app.audit.search(event_type="cost_control"))


class TestPhase7ReleaseHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="omen-phase7-hardening-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_audit_append_fails_closed_after_tampering(self):
        path = self.root / "audit.jsonl"
        log = AuditLog(path)
        log.append({"tool": "test.safe", "status": "ok"})
        path.write_text(path.read_text(encoding="utf-8").replace('"status":"ok"', '"status":"changed"'), encoding="utf-8")
        self.assertFalse(log.verify_integrity())
        with self.assertRaises(AuditError):
            log.append({"tool": "test.after_tamper", "status": "ok"})

    def test_audit_instances_append_to_one_chain_and_redact_all_payload_sections(self):
        path = self.root / "audit.jsonl"
        left, right = AuditLog(path), AuditLog(path)
        threads = [
            threading.Thread(target=lambda log=log, name=name: [
                log.append({"tool": name, "status": "ok", "tokens": {"api_key": "sk-RELEASEFAKE000000000"},
                            "resources": {"password": "do-not-store-this"}})
                for _ in range(10)
            ])
            for log, name in ((left, "left"), (right, "right"))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertTrue(left.verify_integrity())
        contents = path.read_text(encoding="utf-8")
        self.assertNotIn("sk-RELEASEFAKE000000000", contents)
        self.assertNotIn("do-not-store-this", contents)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(left._read_records()), 20)

    def test_backup_is_private_consistent_and_restorable(self):
        source = OmenMemoryStore(db_path=self.root / "source" / "memory.db")
        restored: OmenMemoryStore | None = None
        try:
            source.remember("backup survives restart", wait_for_index=True)
            source.filestore.save_file("local text file", "note.txt")
            archive = source.backup()
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
            restored_path = self.root / "restored" / "memory.db"
            restored_path.parent.mkdir(parents=True)
            restored = OmenMemoryStore(db_path=restored_path)
            info = restored.filestore.restore_backup_archive(archive, restored_path)
            self.assertTrue(info["db_restored"])
            self.assertEqual(restored.recall("backup survives restart")[0]["text"], "backup survives restart")
            self.assertEqual(restored.filestore.read_file("note.txt"), b"local text file")
            with self.assertRaises(FileExistsError):
                source.filestore.create_backup_archive(source.db_path, archive.name)
        finally:
            if restored is not None:
                restored.close()
            source.close()

    def test_backup_restore_rejects_path_traversal_and_links(self):
        store = OmenMemoryStore(db_path=self.root / "memory.db")
        try:
            malicious = self.root / "malicious.tar.gz"
            with tarfile.open(malicious, "w:gz") as archive:
                item = tarfile.TarInfo("files/../../outside.txt")
                payload = b"should not escape"
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
            with self.assertRaises(ValueError):
                store.filestore.restore_backup_archive(malicious, self.root / "restored.db")
            self.assertFalse((self.root.parent / "outside.txt").exists())

            linked = self.root / "linked.tar.gz"
            with tarfile.open(linked, "w:gz") as archive:
                item = tarfile.TarInfo("files/link")
                item.type = tarfile.SYMTYPE
                item.linkname = "../../outside.txt"
                archive.addfile(item)
            with self.assertRaises(ValueError):
                store.filestore.restore_backup_archive(linked, self.root / "linked.db")
        finally:
            store.close()


class TestPhase7ResourceSoak(unittest.TestCase):
    """Concurrent local workload; longer release soak is selectable by environment."""

    def test_chat_voice_indexing_and_scheduled_workload_stays_bounded(self):
        duration = max(1.0, min(300.0, float(os.environ.get("OMEN_PHASE7_SOAK_SECONDS", "5"))))
        with tempfile.TemporaryDirectory(prefix="omen-phase7-soak-") as temp:
            root = Path(temp)
            config = copy.deepcopy(load_config(ROOT))
            config.sections["tools"]["retry"]["backoff_seconds"] = 0
            adapter = FakeAdapter("ollama", "steady local reply")
            app = OmenApp(
                config, root=root, output=lambda _text: None,
                adapters={"ollama": adapter, "openai_compatible": FakeAdapter("openai_compatible", local=False)},
            )
            app.voice.audio = MockAudioDevice()
            app.voice.recognizer = FakeRecognizer()
            app.voice.vad = FakeVAD()
            app.voice.synthesizer = FakeSynthesizer()
            app.voice.confirmation = lambda _text, _confidence: True
            config.sections["voice"]["follow_up_enabled"] = False
            app.scheduler.schedule(
                "phase7-soak-notification", 0.05,
                {"kind": "notification", "message": "soak pulse", "channel": "log"},
                trigger={"type": "interval", "seconds": 0.05},
            )

            stop = threading.Event()
            failures: list[BaseException] = []
            totals = {"chat": 0, "voice": 0, "memory": 0, "jobs": 0}
            totals_lock = threading.Lock()

            def worker(name, action, interval=0.0):
                index = 0
                try:
                    while not stop.is_set():
                        action(index)
                        index += 1
                        with totals_lock:
                            totals[name] = index
                        if interval > 0:
                            stop.wait(interval)
                except BaseException as exc:
                    failures.append(exc)
                    stop.set()

            workers = [
                threading.Thread(target=worker, args=("chat", lambda _i: app.chat("Keep the answer concise.")), daemon=True),
                threading.Thread(target=worker, args=("voice", lambda _i: app.voice.process_sample(root / "mock.wav")), daemon=True),
                threading.Thread(target=worker, args=("memory", lambda i: app.memory.remember(f"soak memory sample {i}")), daemon=True),
                threading.Thread(target=worker, args=("jobs", lambda _i: app.scheduler.tick(), 0.1), daemon=True),
            ]
            started = time.monotonic()
            for thread in workers:
                thread.start()
            while time.monotonic() - started < duration and not stop.wait(0.05):
                pass
            stop.set()
            for thread in workers:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "soak workload worker did not stop")
            try:
                self.assertEqual(failures, [])
                self.assertGreater(totals["chat"], 0)
                self.assertGreater(totals["voice"], 0)
                self.assertGreater(totals["memory"], 0)
                self.assertGreater(totals["jobs"], 0)
                self.assertTrue(app.memory.flush_indexer(timeout=5))
                self.assertLessEqual(app.models.status()["estimated_gb"], 9.0)
                max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                rss_gb = max_rss / (1024 * 1024) if os.name == "posix" else max_rss / (1024**3)
                self.assertLess(rss_gb, 8.0, f"test process peak RSS {rss_gb:.2f} GiB exceeds Omen ceiling")
                self.assertTrue(app.audit.verify_integrity())
                if duration > 5:
                    print(
                        f"Phase 7 soak: {duration:.0f}s; chats={totals['chat']}; voice={totals['voice']}; "
                        f"memory_writes={totals['memory']}; scheduler_ticks={totals['jobs']}; "
                        f"peak_rss={rss_gb:.3f} GiB; estimated_models={app.models.status()['estimated_gb']:.3f} GiB"
                    )
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
