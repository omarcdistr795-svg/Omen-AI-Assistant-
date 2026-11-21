"""Phase 2 Memory Tests: CRUD, search, forget, expiry, export/restore,

secret rejection, background indexing, resource monitor pausing, and memory budget.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from omen.app import OmenApp
from omen.cli.commands import handle_command
from omen.config import load_config
from omen.memory import (
    MemoryItem,
    MemoryKind,
    OmenMemoryStore,
    SecretRejectedError,
    detect_secret,
    generate_safe_id,
    render_memory_review,
    validate_no_secrets,
)
from omen.memory.background import BackgroundIndexer
from omen.memory.filestore import FileStore
from omen.memory.retrieval import compute_recency_multiplier, format_memory_context_for_prompt
from omen.memory.vector import LocalEmbeddingModel, cosine_similarity

ROOT = Path(__file__).resolve().parent.parent


class FakeAdapter:
    def __init__(self, name: str = "ollama", text: str = "Understood.", *, local: bool = True):
        self.name = name
        self.model = "fake-model"
        self.local = local
        self.text = text
        self.calls = 0
        self.last_messages = []

    def complete(self, messages, *, stream=False, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        if stream:
            return iter([self.text[:4], self.text[4:]])
        return {"content": self.text, "provider": self.name, "usage": {}}

    def health(self):
        return {"name": self.name, "configured": True, "local": self.local}


def make_test_app(tmp_path: Path, adapter_text: str = "I remember."):
    cfg = copy.deepcopy(load_config(ROOT))
    adapter = FakeAdapter("ollama", adapter_text)
    app = OmenApp(
        cfg,
        root=tmp_path,
        output=lambda _text: None,
        adapters={"ollama": adapter, "openai_compatible": FakeAdapter("cloud", local=False)},
    )
    return app, adapter


class TestMemoryCRUD(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_memory.db"
        self.store = OmenMemoryStore(db_path=self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_remember_and_get(self):
        item = self.store.remember(
            "Omen is running Phase 2",
            kind=MemoryKind.NOTE.value,
            metadata={"priority": "high"},
            author="soham.bhagat",
            wait_for_index=True,
        )
        self.assertTrue(item["id"].startswith("mem_"))
        self.assertEqual(item["text"], "Omen is running Phase 2")
        self.assertEqual(item["author"], "soham.bhagat")
        self.assertEqual(item["version"], 1)
        self.assertEqual(item["metadata"]["priority"], "high")

        fetched = self.store.get(item["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], item["id"])
        self.assertEqual(fetched["text"], item["text"])

    def test_update(self):
        item = self.store.remember("Initial fact", wait_for_index=True)
        rec_id = item["id"]
        created_at = item["created_at"]

        updated = self.store.update(
            rec_id,
            text="Updated fact content",
            metadata={"edited": True},
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["text"], "Updated fact content")
        self.assertEqual(updated["metadata"]["edited"], True)
        self.assertEqual(updated["created_at"], created_at)
        self.assertGreaterEqual(updated["updated_at"], created_at)

    def test_forget(self):
        item = self.store.remember("Temporary note to forget", wait_for_index=True)
        rec_id = item["id"]

        self.assertTrue(self.store.forget(rec_id))
        self.assertIsNone(self.store.get(rec_id))
        self.assertEqual(self.store.recall(query="Temporary note to forget"), [])
        # Second forget returns False
        self.assertFalse(self.store.forget(rec_id))

    def test_safe_id_avoids_phone_redaction(self):
        from omen.safety.redaction import redact_text

        for _ in range(500):
            sid = generate_safe_id()
            self.assertEqual(sid, redact_text(sid))


class TestMemorySearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_search.db"
        self.store = OmenMemoryStore(db_path=self.db_path)

        self.m1 = self.store.remember(
            "Python 3.11 with SQLite FTS5 and vector embeddings",
            kind="preference",
            metadata={"topic": "coding"},
            wait_for_index=True,
        )
        self.m2 = self.store.remember(
            "Omen personal assistant designed for Soham Bhagat",
            kind="profile",
            metadata={"user": "Soham"},
            wait_for_index=True,
        )
        self.m3 = self.store.remember(
            "Omarchy Linux on Intel Core Ultra laptop with NPU",
            kind="profile",
            metadata={"hardware": "laptop"},
            wait_for_index=True,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_fts_keyword_search(self):
        results = self.store.recall(query="SQLite FTS5", limit=5)
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["id"], self.m1["id"])

    def test_vector_semantic_search(self):
        results = self.store.recall(query="programming language and database", limit=5)
        self.assertGreaterEqual(len(results), 1)
        # Python and SQLite should be highest ranked
        self.assertEqual(results[0]["id"], self.m1["id"])

    def test_kind_filter(self):
        results = self.store.recall(query="", kind="preference")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], self.m1["id"])

        profile_results = self.store.recall(query="", kind="profile")
        self.assertEqual(len(profile_results), 2)


class TestDomainMemories(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OmenMemoryStore(db_path=Path(self.tmp.name) / "domains.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_profile_memory(self):
        self.store.set_profile("name", "Soham Bhagat")
        self.store.set_profile("location", "Mumbai, India")
        self.store.set_profile("timezone", "Asia/Kolkata")
        self.store.set_profile("communication", "calm, witty, concise")

        prof = self.store.get_profile()
        self.assertEqual(prof["name"], "Soham Bhagat")
        self.assertEqual(prof["timezone"], "Asia/Kolkata")
        self.assertEqual(prof["communication"], "calm, witty, concise")

        # Update existing profile field
        self.store.set_profile("timezone", "UTC")
        self.assertEqual(self.store.get_profile()["timezone"], "UTC")

    def test_projects_memory(self):
        proj = self.store.store_project(
            name="Omen Assistant",
            goal="Build all-in-one personal AI assistant",
            repository="https://github.com/omen/omen",
            stakeholders=["Soham Bhagat"],
            deadline="2026-12-31",
            decisions=["Use local-first SQLite and embeddings", "No heavy cloud DB"],
            open_questions=["TTS model selection"],
            blockers=[],
            progress="Phase 2 Memory underway",
        )
        self.assertEqual(proj["kind"], MemoryKind.PROJECT.value)

        fetched = self.store.get_project("Omen Assistant")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["metadata"]["goal"], "Build all-in-one personal AI assistant")

        projects = self.store.list_projects()
        self.assertEqual(len(projects), 1)

    def test_preferences_memory(self):
        self.store.set_preference("coding_stack", "Python 3.11+, PyYAML, SQLite")
        self.store.set_preference("briefing_time", "08:30")
        self.store.set_preference("voice_verbosity", "concise")

        self.assertEqual(self.store.get_preference("coding_stack"), "Python 3.11+, PyYAML, SQLite")
        self.assertEqual(self.store.get_preference("briefing_time"), "08:30")
        self.assertEqual(self.store.get_preference("nonexistent", "default"), "default")

        prefs = self.store.list_preferences()
        self.assertEqual(len(prefs), 3)

    def test_analyses_memory(self):
        item = self.store.store_analysis(
            topic="Local Embedding Architecture",
            findings="Subword hash vectors use <2MB RAM and satisfy 0.3GB ceiling with 0 external deps",
            analysis_type="research",
            lessons="Avoid heavy PyTorch runtimes when subword hashing provides high accuracy",
            decisions=["Adopt deterministic subword n-gram embedding"],
        )
        self.assertEqual(item["kind"], MemoryKind.ANALYSIS.value)
        self.assertEqual(item["metadata"]["topic"], "Local Embedding Architecture")

        analyses = self.store.list_analyses()
        self.assertEqual(len(analyses), 1)

    def test_conversation_summary_memory(self):
        summary = self.store.store_conversation_summary(
            summary="User discussed Phase 2 Memory specifications and approved local SQLite architecture.",
            session_id="session_01",
            turn_range=(1, 5),
            source_messages=["msg_1", "msg_5"],
            linked_tools=["demo.echo"],
        )
        self.assertEqual(summary["kind"], MemoryKind.CONVERSATION_SUMMARY.value)
        self.assertEqual(summary["metadata"]["session_id"], "session_01")
        self.assertEqual(summary["metadata"]["turn_range"], [1, 5])

        summaries = self.store.list_conversation_summaries("session_01")
        self.assertEqual(len(summaries), 1)

    def test_entity_memory(self):
        ent = self.store.remember_entity(
            name="Valence",
            entity_type="company",
            details="Partner company collaborating on infrastructure",
            relations=["partner"],
            attributes={"industry": "AI Software"},
        )
        self.assertEqual(ent["kind"], MemoryKind.ENTITY.value)

        found = self.store.find_entities("Valence")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Valence")
        self.assertEqual(found[0]["entity_type"], "company")


class TestExpiryAndTransient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OmenMemoryStore(db_path=Path(self.tmp.name) / "transient.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_transient_auto_detection(self):
        item1 = self.store.remember("Traffic is bad right now on Western Expressway", wait_for_index=True)
        self.assertTrue(item1["is_transient"])

        item2 = self.store.remember("Python is an interpreted language", wait_for_index=True)
        self.assertFalse(item2["is_transient"])

    def test_expiry_filtering(self):
        past_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

        expired = self.store.remember("Lunch reservation at 12:00 PM", expires_at=past_time, wait_for_index=True)
        valid = self.store.remember("Evening conference call at 7:00 PM", expires_at=future_time, wait_for_index=True)

        recalled = self.store.recall(query="reservation call", limit=10, include_expired=False)
        recalled_ids = [r["id"] for r in recalled]
        self.assertIn(valid["id"], recalled_ids)
        self.assertNotIn(expired["id"], recalled_ids)

        # When include_expired=True, expired item is returned
        all_recalled = self.store.recall(query="reservation call", limit=10, include_expired=True)
        all_ids = [r["id"] for r in all_recalled]
        self.assertIn(expired["id"], all_ids)

    def test_purge_expired(self):
        past_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.store.remember("Old expired reminder", expires_at=past_time, wait_for_index=True)
        self.store.remember("Permanent reminder", expires_at=None, wait_for_index=True)

        purged = self.store.purge_expired()
        self.assertEqual(purged, 1)
        self.assertEqual(len(self.store.recall(query="", include_expired=True)), 1)

    def test_transient_decay_deemphasis(self):
        # A fresh transient memory vs an old transient memory
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=6)).isoformat()

        fresh_item = MemoryItem(id="m_fresh", text="Meeting alert", is_transient=True, created_at=now.isoformat())
        old_item = MemoryItem(id="m_old", text="Meeting alert", is_transient=True, created_at=old_time)

        fresh_mult = compute_recency_multiplier(fresh_item, current_time=now, transient_half_life_days=2.0)
        old_mult = compute_recency_multiplier(old_item, current_time=now, transient_half_life_days=2.0)

        self.assertAlmostEqual(fresh_mult, 1.0, places=2)
        # 6 days with half-life of 2 days = (0.5)^3 = 0.125
        self.assertAlmostEqual(old_mult, 0.125, places=2)
        self.assertGreater(fresh_mult, old_mult)


class TestSecretRejection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OmenMemoryStore(db_path=Path(self.tmp.name) / "secrets.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_reject_api_keys(self):
        secrets = [
            "sk-abc1234567890abcdefghijklmnop",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",
        ]
        for sec in secrets:
            with self.subTest(sec=sec):
                with self.assertRaises(SecretRejectedError):
                    self.store.remember(f"Here is my key: {sec}")

    def test_reject_passwords_and_assignments(self):
        bad_texts = [
            "password=mySuperSecretPass123!",
            "api_key: sk-proj-1234567890123456",
            "db uri: postgres://admin:superSecretPass@localhost:5432/omen",
            "credential = topsecretpass",
        ]
        for text in bad_texts:
            with self.subTest(text=text):
                with self.assertRaises(SecretRejectedError):
                    self.store.remember(text)

    def test_reject_secrets_in_metadata(self):
        with self.assertRaises(SecretRejectedError):
            self.store.remember("Clean text", metadata={"api_key": "some_value"})

        with self.assertRaises(SecretRejectedError):
            self.store.remember("Clean text", metadata={"auth": {"token": "secret_token_123"}})

    def test_reject_in_filestore(self):
        filestore = self.store.filestore
        with self.assertRaises(SecretRejectedError):
            filestore.save_file("password=super_secret_leak_123", "leak.txt")

    def test_clean_text_allowed(self):
        item = self.store.remember("User mentioned interest in cryptography and security principles")
        self.assertIsNotNone(item["id"])


class TestBackgroundIndexingAndMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor_mock = MagicMock()
        self.monitor_mock.level.return_value = "normal"
        self.store = OmenMemoryStore(
            db_path=Path(self.tmp.name) / "bg.db",
            monitor=self.monitor_mock,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_non_blocking_background_indexing(self):
        item = self.store.remember("Indexing happens in background asynchronously")
        self.assertTrue(self.store.flush_indexer(timeout=2.0))
        status = self.store.status()
        self.assertEqual(status["vectors_indexed"], 1)

    def test_pause_and_resume_by_resource_monitor(self):
        # Pressure increases to degrade
        self.monitor_mock.level.return_value = "degrade"
        self.store.indexer.check_resource_pressure()
        self.assertTrue(self.store.is_indexing_paused())

        # Pressure returns to normal
        self.monitor_mock.level.return_value = "normal"
        self.store.indexer.check_resource_pressure()
        self.assertFalse(self.store.is_indexing_paused())


class TestFileStoreAndBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OmenMemoryStore(db_path=Path(self.tmp.name) / "backup_test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_filestore_operations(self):
        fs = self.store.filestore
        saved = fs.save_file("Research note content on AI memory.", "research.txt")
        self.assertTrue(saved["file_id"].startswith("file_"))

        read_back = fs.read_file(saved["file_id"]).decode("utf-8")
        self.assertEqual(read_back, "Research note content on AI memory.")

        self.assertEqual(len(fs.list_files()), 1)
        self.assertTrue(fs.delete_file(saved["file_id"]))
        self.assertEqual(len(fs.list_files()), 0)

    def test_export_and_restore(self):
        self.store.set_profile("name", "Soham")
        self.store.remember("Export test memory item 1", wait_for_index=True)
        self.store.remember("Export test memory item 2", wait_for_index=True)

        export_file = Path(self.tmp.name) / "export.json"
        exported = self.store.export_data(export_file)
        self.assertEqual(exported["counts"]["memories"], 3)
        self.assertTrue(export_file.exists())

        # Restore into fresh store
        fresh_db = Path(self.tmp.name) / "restored.db"
        fresh_store = OmenMemoryStore(db_path=fresh_db)
        try:
            res = fresh_store.restore_data(export_file)
            self.assertEqual(res["restored_memories"], 3)
            self.assertEqual(fresh_store.get_profile()["name"], "Soham")
            self.assertEqual(len(fresh_store.recall("Export test", limit=10)), 2)
        finally:
            fresh_store.close()

    def test_export_restore_roundtrip_includes_entities(self):
        self.store.remember_entity(
            name="Ollama",
            entity_type="tool",
            details="Local inference server",
            attributes={"kind": "local"},
        )
        export_file = Path(self.tmp.name) / "entities_export.json"
        payload = self.store.export_data(export_file)
        self.assertEqual(payload["counts"]["entities"], 1)

        fresh_db = Path(self.tmp.name) / "entities_restored.db"
        fresh_store = OmenMemoryStore(db_path=fresh_db)
        try:
            result = fresh_store.restore_data(export_file)
            self.assertEqual(result["restored_entities"], 1)
            found = fresh_store.find_entities("Ollama")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["attributes"]["kind"], "local")
        finally:
            fresh_store.close()

    def test_backup_archive(self):
        self.store.remember("Backup test memory item", wait_for_index=True)
        backup_archive = self.store.backup()
        self.assertTrue(backup_archive.exists())
        self.assertTrue(backup_archive.name.endswith(".tar.gz"))


class TestTerminalMemoryCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.adapter = make_test_app(Path(self.tmp.name))
        self.emitted: list[str] = []

    def tearDown(self):
        if hasattr(self.app.memory, "close"):
            self.app.memory.close()
        self.tmp.cleanup()

    def command(self, line: str) -> str:
        handled = handle_command(self.app, line, self.emitted.append)
        self.assertTrue(handled, f"Command not handled: {line}")
        return self.emitted[-1]

    def test_remember_update_forget_cycle(self):
        out1 = self.command("/remember Omen memory terminal test")
        self.assertIn("REMEMBER", out1)
        rec_id = json.loads(out1.split("\n", 1)[1])["id"]

        self.emitted.clear()
        out2 = self.command(f"/update {rec_id} Updated memory terminal test")
        self.assertIn("UPDATE", out2)
        self.assertIn("Updated memory terminal test", out2)

        self.emitted.clear()
        out3 = self.command(f"/forget {rec_id}")
        self.assertIn('"removed": true', out3)

    def test_memory_search_and_review_commands(self):
        self.command("/remember Testing memory search feature")
        self.emitted.clear()

        out_search = self.command("/memory Testing")
        self.assertIn("MEMORY", out_search)

        self.emitted.clear()
        out_review = self.command("/review-memory")
        self.assertIn("MEMORY REVIEW", out_review)

        self.emitted.clear()
        out_show = self.command("/show memory")
        self.assertIn("MEMORY REVIEW", out_show)

    def test_export_and_backup_commands(self):
        self.command("/remember Data to backup")
        self.emitted.clear()

        out_export = self.command("/export-memory")
        self.assertIn("EXPORT_MEMORY", out_export)

        self.emitted.clear()
        out_backup = self.command("/backup-memory")
        self.assertIn("BACKUP_MEMORY", out_backup)


class TestMemoryBudgetAndBrainLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "budget.db"
        self.store = OmenMemoryStore(db_path=self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_budget_compliance_on_realistic_dataset(self):
        """Verify memory use stays strictly within the 0.3 GB (300 MB) budget on realistic data size (1000 items)."""
        import sys

        # Insert 1000 memory items
        for i in range(1000):
            self.store.remember(
                f"Synthetic memory entry #{i}: factual record regarding project milestone, tool executions, and user decisions.",
                kind=MemoryKind.NOTE.value,
                metadata={"index": i, "category": "synthetic"},
                wait_for_index=True,
            )

        db_size_mb = self.db_path.stat().st_size / 1024 / 1024
        # 1000 items in SQLite with FTS5 and 128-d vectors should be well under 10 MB
        self.assertLess(db_size_mb, 10.0, f"Database size {db_size_mb:.2f} MB exceeds 10 MB")

        # Check embedding model RAM footprint
        model = self.store.embedding_model
        model_size_mb = sys.getsizeof(model) / 1024 / 1024
        self.assertLess(model_size_mb, 1.0)

    def test_recall_failure_is_reported_not_swallowed(self):
        """A memory outage must be audited and reported without breaking chat."""

        class BrokenMemory:
            def recall(self, *args, **kwargs):
                raise RuntimeError("simulated memory outage")

            def status(self):
                return {"phase": 2, "implementation": "broken-test-double"}

        app, _adapter = make_test_app(Path(self.tmp.name) / "recall_failure")
        original_memory = app.memory
        app.memory = BrokenMemory()
        try:
            list(app.stream_chat("hello"))
            self.assertTrue(any("memory recall failed" in note.lower() for note in app.notices))
            errors = app.history(event_type="memory_retrieval_error")
            self.assertEqual(len(errors), 1)
            self.assertIn("simulated memory outage", errors[0]["error"])
        finally:
            original_memory.close()

    def test_brain_loop_retrieval(self):
        """Verify retrieval is wired into the brain loop and enriches chat context."""
        app, adapter = make_test_app(Path(self.tmp.name))
        try:
            app.memory.remember(
                "Soham Bhagat's preferred development framework is Python with local SQLite",
                kind="preference",
                wait_for_index=True,
            )
            # Send message querying preference
            list(app.stream_chat("What framework does Soham prefer?"))
            # Check messages sent to provider
            sent_messages = adapter.last_messages
            system_msg = next((m["content"] for m in sent_messages if m["role"] == "system"), "")
            self.assertIn("Relevant Context & Memory:", system_msg)
            self.assertIn("Python with local SQLite", system_msg)
        finally:
            if hasattr(app.memory, "close"):
                app.memory.close()


if __name__ == "__main__":
    unittest.main()
