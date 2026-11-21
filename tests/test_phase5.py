"""Phase 5 tests: scheduling, notifications, briefings, alerts and safety regression."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from omen.app import OmenApp
from omen.audit.log import AuditLog
from omen.automation import (
    AutomationDataStore,
    BriefingService,
    BuildAlerts,
    ContextualReminders,
    DeadlineGuardian,
    Notifier,
    Scheduler,
)
from omen.cli.commands import handle_command
from omen.config import load_config

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)


def fixed_clock(current: dict[str, datetime]):
    return lambda: current["value"]


def make_audit(tmp: Path, clock=None) -> AuditLog:
    return AuditLog(tmp / "logs" / "audit.jsonl")


def make_config(tmp: Path | None = None) -> object:
    cfg = copy.deepcopy(load_config(ROOT))
    return cfg


class Clock:
    def __init__(self, value: datetime = NOW):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.audit = make_audit(self.root)
        self.cfg = make_config(self.root)
        self.scheduler = Scheduler(self.cfg, root=self.root, audit=self.audit, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schedule_one_shot_and_cancel(self):
        job = self.scheduler.schedule("brief", NOW.isoformat(), {"kind": "briefing"})
        self.assertEqual(job["status"], "scheduled")
        self.assertEqual(len(self.scheduler.list_jobs()), 1)
        self.assertTrue(self.scheduler.cancel(job["id"]))
        self.assertEqual(self.scheduler.list_jobs()[0]["status"], "cancelled")
        self.assertFalse(self.scheduler.cancel(job["id"]))

    def test_daily_trigger_runs_at_configured_time(self):
        job = self.scheduler.schedule("daily-brief", "09:00", {"kind": "briefing"})
        self.assertEqual(job["trigger"]["type"], "daily")
        self.clock.value = datetime(2026, 11, 22, 8, 59, tzinfo=timezone.utc)
        self.assertEqual(self.scheduler.tick(), [])
        self.clock.value = datetime(2026, 11, 22, 9, 1, tzinfo=timezone.utc)
        results = self.scheduler.tick()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(self.scheduler.list_jobs()[0]["status"], "scheduled")

    def test_interval_trigger_repeats(self):
        self.scheduler.schedule("poll", 60, {"kind": "build_scan"})
        self.clock.advance(seconds=60)
        first = self.scheduler.tick(handler=lambda job: {"ok": True})
        self.assertEqual(first[0]["status"], "ok")
        self.clock.advance(minutes=1)
        second = self.scheduler.tick(handler=lambda job: {"ok": True})
        self.assertEqual(second[0]["status"], "ok")
        self.assertGreater(self.scheduler.list_jobs()[0]["attempts"], 0)

    def test_retry_succeeds_with_exponential_backoff(self):
        self.scheduler.schedule("flaky", NOW.isoformat(), max_retries=3)
        attempts = {"value": 0}

        def flaky(_job):
            attempts["value"] += 1
            if attempts["value"] < 3:
                raise RuntimeError("temporary failure")
            return {"recovered": True}

        first = self.scheduler.tick(handler=flaky)
        self.assertEqual(first[0]["status"], "retry_scheduled")
        self.assertEqual(first[0]["retry_in_seconds"], 5)
        self.clock.advance(seconds=5)
        second = self.scheduler.tick(handler=flaky)
        self.assertEqual(second[0]["status"], "retry_scheduled")
        self.assertEqual(second[0]["retry_in_seconds"], 10)
        self.clock.advance(seconds=10)
        third = self.scheduler.tick(handler=flaky)
        self.assertEqual(third[0]["status"], "ok")
        self.assertEqual(third[0]["attempts"], 3)

    def test_exhausted_retries_fail_and_are_audited(self):
        self.scheduler.schedule("broken", NOW.isoformat(), max_retries=1)

        def broken(_job):
            raise RuntimeError("permanent failure")

        self.scheduler.tick(handler=broken)
        self.clock.advance(seconds=5)
        final = self.scheduler.tick(handler=broken)
        self.assertEqual(final[0]["status"], "failed")
        self.assertIn("permanent failure", final[0]["error"])
        statuses = [event["status"] for event in self.audit.search(event_type="scheduler_job")]
        self.assertIn("retry_scheduled", statuses)
        self.assertIn("failed", statuses)

    def test_heavy_job_defers_under_memory_pressure(self):
        self.scheduler.schedule("reindex", NOW.isoformat(), resource={"heavy": True})
        deferred = self.scheduler.tick(resource_level="degrade", handler=lambda job: {"ran": True})
        self.assertEqual(deferred[0]["status"], "deferred")
        self.assertFalse(deferred[0]["result"])
        self.assertEqual(self.scheduler.status()["deferred_total"], 1)
        self.clock.advance(seconds=60)
        resumed = self.scheduler.tick(resource_level="normal", handler=lambda job: {"ran": True})
        self.assertEqual(resumed[0]["status"], "ok")
        self.assertTrue(resumed[0]["result"]["ran"])

    def test_loaded_model_lowers_job_priority(self):
        self.scheduler.schedule("light", NOW.isoformat())
        results = self.scheduler.tick(loaded_models=["local_reasoning"], handler=lambda job: {"ok": True})
        self.assertEqual(results[0]["priority"], "low")
        normal = self.scheduler.schedule("other", NOW.isoformat())
        self.assertEqual(normal["priority"], "normal")

    def test_event_trigger_and_persistence(self):
        self.scheduler.schedule("on-open", None, {}, trigger={"type": "event", "topic": "repo_opened"})
        results = self.scheduler.fire_event("repo_opened")
        self.assertEqual(results[0]["status"], "ok")
        reloaded = Scheduler(self.cfg, root=self.root, audit=self.audit, clock=self.clock)
        self.assertEqual(len(reloaded.list_jobs()), 1)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.cfg.sections["automation"]["notifications"]["quiet_hours"]["enabled"] = False
        self.cfg.sections["automation"]["notifications"]["channels"]["voice"] = True
        self.clock = Clock()
        self.audit = make_audit(self.root)
        self.output: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def notifier(self, speak=None) -> Notifier:
        return Notifier(
            self.cfg,
            root=self.root,
            audit=self.audit,
            output=self.output.append,
            speak=speak,
            clock=self.clock,
        )

    def test_priority_levels_and_auto_channels(self):
        notifier = self.notifier(speak=lambda text: {"spoken": True, "characters": len(text)})
        low = notifier.notify("low message", priority="low")
        self.assertTrue(low["delivered"])
        self.assertEqual(low["channels"], ["terminal"])
        critical = notifier.notify("critical message", priority="critical", channel="auto")
        self.assertEqual(critical["priority"], "critical")
        self.assertIn("voice", critical["channels"])
        self.assertIn("log", critical["channels"])

    def test_quiet_hours_defer_noncritical_and_flush(self):
        quiet = self.cfg.sections["automation"]["notifications"]["quiet_hours"]
        quiet["enabled"] = True
        quiet["start"] = "22:00"
        quiet["end"] = "07:00"
        self.clock.value = datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc)
        notifier = self.notifier()
        deferred = notifier.notify("nightly note", priority="normal")
        self.assertEqual(deferred["status"], "deferred")
        self.assertFalse(deferred["delivered"])
        urgent = notifier.notify("critical failure", priority="critical", channel="terminal")
        self.assertEqual(urgent["status"], "delivered")
        self.clock.value = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
        flushed = notifier.flush_pending()
        self.assertEqual(len(flushed), 1)
        self.assertTrue(flushed[0]["delivered"])

    def test_deduplication_and_spam_limit(self):
        notifier = self.notifier()
        results = [notifier.notify("same alert", key="dedupe-key") for _ in range(4)]
        self.assertEqual([item["status"] for item in results], ["delivered", "duplicate", "duplicate", "suppressed"])
        status = notifier.status()
        self.assertEqual(status["duplicate"], 2)
        self.assertEqual(status["suppressed"], 1)
        self.assertEqual(status["delivered"], 1)

    def test_hourly_rate_limit_defers_excess(self):
        self.cfg.sections["automation"]["notifications"]["anti_spam"]["rate_limit_per_hour"] = 2
        notifier = self.notifier()
        results = [notifier.notify(f"distinct {index}", key=f"k-{index}") for index in range(3)]
        self.assertEqual([item["status"] for item in results], ["delivered", "delivered", "deferred"])

    def test_voice_failure_falls_back_to_terminal_and_is_audited(self):
        def broken_speak(_text):
            raise RuntimeError("no audio device")

        notifier = self.notifier(speak=broken_speak)
        item = notifier.notify("speak this", channel="voice")
        self.assertTrue(item["delivered"])
        self.assertTrue(any("no audio device" in entry for entry in item["fallbacks"]))
        self.assertTrue(any(event.get("event_type") == "notification_fallback" for event in self.audit.search()))
        self.assertTrue(any("speak this" in entry for entry in self.output))

    def test_voice_absent_falls_back_without_error(self):
        notifier = self.notifier(speak=None)
        item = notifier.notify("speak this", channel="voice")
        self.assertTrue(item["delivered"])
        self.assertTrue(any("voice not present" in entry for entry in item["fallbacks"]))

    def test_no_secret_is_written_to_audit_log(self):
        notifier = self.notifier()
        notifier.notify("token sk-FAKEONLYVALUE0000 used", channel="log")
        text = (self.root / "logs" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("sk-FAKEONLYVALUE0000", text)
        self.assertIn("REDACTED", text)


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.clock = Clock()
        self.audit = make_audit(self.root)
        self.store = AutomationDataStore(self.cfg, self.root)
        self.store.create("calendar", {
            "title": "Standup",
            "start": "2026-11-21T09:30:00+00:00",
            "end": "2026-11-21T10:00:00+00:00",
        })
        self.store.create("calendar", {
            "title": "Planning",
            "start": "2026-11-22T10:00:00+00:00",
            "end": "2026-11-22T11:00:00+00:00",
        })
        self.store.create("tasks", {
            "title": "Ship phase 5", "status": "todo",
            "due_at": "2026-11-22T12:00:00+00:00", "project": "Omen",
        })
        self.store.create("tasks", {"title": "Blocked on key", "status": "blocked"})
        self.store.create("reminders", {
            "title": "Call Sam", "due_at": "2026-11-21T09:45:00+00:00",
        })
        self.store.create("notes", {
            "title": "Decision on storage", "text": "we chose sqlite", "status": "active",
        })
        self.audit.append({
            "timestamp": "2026-11-21T08:05:00+00:00",
            "event_type": "tool_call", "tool": "files.read", "status": "ok",
            "summary": "files.read: ok",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def service(self) -> BriefingService:
        return BriefingService(
            self.cfg, data_store=self.store, audit=self.audit,
            alerts=lambda: [{"type": "ci_failure", "priority": "high"}], clock=self.clock,
        )

    def test_morning_briefing_has_all_sections_and_real_data(self):
        result = self.service().morning(now=self.clock.value)
        sections = result["sections"]
        for name in ("calendar", "tasks", "deadlines", "weather", "project_blockers"):
            self.assertIn(name, sections)
        titles = [item.get("title") for item in sections["calendar"]["items"]]
        self.assertIn("Standup", titles)
        self.assertTrue(any(item.get("title") == "Ship phase 5" for item in sections["tasks"]["items"]))
        self.assertTrue(any(item.get("title") == "Call Sam" for item in sections["deadlines"]["items"]))
        self.assertTrue(any(item.get("source") == "blocked_task" for item in sections["project_blockers"]["items"]))
        self.assertEqual(sections["weather"]["status"], "disabled")
        self.assertTrue(any(event.get("event_type") == "briefing" for event in self.audit.search()))

    def test_weather_enabled_without_connector_is_blocked_external(self):
        self.cfg.sections["automation"]["briefings"]["weather"]["enabled"] = True
        weather = self.service().morning(now=self.clock.value)["sections"]["weather"]
        self.assertEqual(weather["status"], "BLOCKED-EXTERNAL")
        self.assertNotIn("temperature", weather)
        self.assertIn("no forecast invented", weather["reason"])

    def test_evening_review_has_all_sections(self):
        result = self.service().evening(now=self.clock.value)
        sections = result["sections"]
        for name in ("completed_work", "unresolved_decisions", "tomorrow_priorities", "open_risks"):
            self.assertIn(name, sections)
        self.assertTrue(any(item.get("tool") == "files.read" for item in sections["completed_work"]["items"]))
        self.assertTrue(any(item.get("title") == "Decision on storage" for item in sections["unresolved_decisions"]["items"]))
        self.assertTrue(any(item.get("title") == "Planning" for item in sections["tomorrow_priorities"]["items"]))
        self.assertTrue(any(item.get("source") == "blocked_task" for item in sections["open_risks"]["items"]))

    def test_empty_store_briefing_invents_nothing(self):
        empty_root = Path(tempfile.mkdtemp())
        empty_store = AutomationDataStore(self.cfg, empty_root)
        service = BriefingService(self.cfg, data_store=empty_store, audit=self.audit, clock=self.clock)
        result = service.morning(now=self.clock.value)
        for name, section in result["sections"].items():
            if name != "weather":
                self.assertEqual(section["count"], 0)
        self.assertEqual(result["sections"]["weather"]["status"], "disabled")


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.clock = Clock()
        self.audit = make_audit(self.root)
        self.store = AutomationDataStore(self.cfg, self.root)
        self.output: list[str] = []
        self.notifier = Notifier(
            self.cfg, root=self.root, audit=self.audit,
            output=self.output.append, clock=self.clock,
        )
        self.guardian = DeadlineGuardian(
            self.cfg, root=self.root, data_store=self.store,
            notifier=self.notifier, audit=self.audit, clock=self.clock,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_escalating_reminders_with_checklist_and_no_repeat(self):
        self.store.create("reminders", {
            "title": "Submit report", "due_at": "2026-11-21T09:25:00+00:00",
        })
        first = self.guardian.check(now=self.clock.value)
        self.assertEqual(len(first["reminders"]), 1)
        self.assertEqual(first["reminders"][0]["level"], "lead_30m")
        self.assertEqual(first["reminders"][0]["priority"], "high")
        self.assertEqual(len(first["checklists"][0]["items"]), 4)
        second = self.guardian.check(now=self.clock.value)
        self.assertEqual(second["reminders"], [])
        self.clock.value = datetime(2026, 11, 21, 9, 31, tzinfo=timezone.utc)
        overdue = self.guardian.check(now=self.clock.value)
        self.assertEqual(len(overdue["reminders"]), 1)
        self.assertEqual(overdue["reminders"][0]["level"], "overdue")
        self.assertEqual(overdue["reminders"][0]["priority"], "critical")

    def test_conflict_detection_between_meetings_and_deadline(self):
        self.store.create("calendar", {
            "title": "Design review", "start": "2026-11-21T10:00:00+00:00", "end": "2026-11-21T11:00:00+00:00",
        })
        self.store.create("calendar", {
            "title": "Interview", "start": "2026-11-21T10:30:00+00:00", "end": "2026-11-21T11:30:00+00:00",
        })
        self.store.create("reminders", {
            "title": "Send notes", "due_at": "2026-11-21T10:15:00+00:00",
        })
        report = self.guardian.check(now=self.clock.value)
        types = {conflict["type"] for conflict in report["conflicts"]}
        self.assertIn("overlapping_meetings", types)
        self.assertIn("deadline_during_meeting", types)

    def test_custom_checklist_is_used_when_provided(self):
        self.store.create("tasks", {
            "title": "Deploy", "status": "todo",
            "due_at": "2026-11-21T09:20:00+00:00",
            "checklist": ["run tests", "tag release"],
        })
        report = self.guardian.check(now=self.clock.value)
        self.assertEqual(report["checklists"][0]["items"], ["run tests", "tag release"])


class BuildAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.clock = Clock()
        self.audit = make_audit(self.root)
        self.output: list[str] = []
        self.notifier = Notifier(
            self.cfg, root=self.root, audit=self.audit,
            output=self.output.append, clock=self.clock,
        )
        self.alerts = BuildAlerts(self.cfg, root=self.root, notifier=self.notifier, audit=self.audit, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_ci_deployment_and_long_job(self):
        ci = self.alerts.ingest({"source": "ci", "status": "failed", "job": "build-42"})
        deployment = self.alerts.ingest({"source": "deployment", "status": "failed", "job": "prod"})
        long_job = self.alerts.ingest({"source": "job", "status": "ok", "job": "nightly", "duration_ms": 400000})
        self.assertEqual([ci["type"], deployment["type"], long_job["type"]], ["ci_failure", "deployment_failure", "long_running_job"])
        self.assertEqual(long_job["priority"], "normal")
        self.assertEqual(len(self.alerts.summary()), 3)
        self.assertTrue(any("CI job build-42 failed" in entry for entry in self.output))

    def test_scan_detects_regression_once(self):
        self.audit.append({
            "timestamp": "2026-11-21T08:00:00+00:00", "event_type": "tool_call",
            "tool": "tests.run", "status": "ok", "args": {"command": "python3 -m unittest"},
            "summary": "tests.run: ok",
        })
        self.audit.append({
            "timestamp": "2026-11-21T08:10:00+00:00", "event_type": "tool_call",
            "tool": "tests.run", "status": "error", "args": {"command": "python3 -m unittest"},
            "summary": "tests.run: error",
        })
        first = self.alerts.scan(now=self.clock.value)
        second = self.alerts.scan(now=self.clock.value)
        self.assertEqual(first["alerts_found"], 1)
        self.assertEqual(first["emitted"], 1)
        self.assertEqual(second["emitted"], 0)
        self.assertEqual(self.alerts.summary()[0]["type"], "test_regression")


class ContextualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.clock = Clock()
        self.audit = make_audit(self.root)
        self.store = AutomationDataStore(self.cfg, self.root)
        self.output: list[str] = []
        self.notifier = Notifier(
            self.cfg, root=self.root, audit=self.audit,
            output=self.output.append, clock=self.clock,
        )
        self.scheduler = Scheduler(self.cfg, root=self.root, audit=self.audit, clock=self.clock)
        self.contextual = ContextualReminders(
            self.cfg, root=self.root, data_store=self.store,
            scheduler=self.scheduler, notifier=self.notifier,
            audit=self.audit, clock=self.clock,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_trigger_fires_only_for_matching_repository(self):
        repo = self.root / "repos" / "omen"
        reminder = self.contextual.add("Check the CI status", repo=str(repo))
        fired = self.contextual.on_repo_open(str(repo / "src"), now=self.clock.value)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["reminder_id"], reminder["id"])
        self.assertEqual(self.contextual.on_repo_open(str(self.root / "elsewhere")), [])
        self.assertEqual(self.contextual.list()[0]["fired_count"], 1)

    def test_before_next_meeting_schedules_or_notifies(self):
        self.store.create("calendar", {
            "title": "Roadmap", "start": "2026-11-21T11:00:00+00:00", "end": "2026-11-21T12:00:00+00:00",
        })
        scheduled = self.contextual.before_next_meeting(now=self.clock.value)
        self.assertEqual(scheduled["status"], "scheduled")
        self.assertTrue(scheduled["job_id"])
        self.store.create("calendar", {
            "title": "Quick sync", "start": "2026-11-21T09:05:00+00:00", "end": "2026-11-21T09:15:00+00:00",
        })
        immediate = self.contextual.before_next_meeting(now=self.clock.value)
        self.assertEqual(immediate["status"], "notified_now")
        self.assertTrue(any("Quick sync" in entry for entry in self.output))

    def test_no_upcoming_meeting_and_cancel(self):
        self.assertEqual(self.contextual.before_next_meeting(now=self.clock.value)["status"], "no_upcoming_meeting")
        reminder = self.contextual.add("Ask about tests", meeting="standup")
        self.assertTrue(self.contextual.cancel(reminder["id"]))
        self.assertEqual(self.contextual.list(), [])


class SafetyAndIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.cfg.sections["tools"]["retry"]["backoff_seconds"] = 0
        self.output: list[str] = []
        self.app = OmenApp(self.cfg, root=self.root, output=self.output.append)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_phase5_tools_declare_permissions_and_are_allowlisted(self):
        schemas = self.app.tools.schemas()
        for name in (
            "brief.morning", "brief.evening", "status.report", "guardian.check",
            "build_alerts.scan", "automation.schedule", "automation.cancel",
            "notify.send", "contextual.remind",
        ):
            self.assertIn(name, schemas)
            self.assertIn(schemas[name]["permission_class"], ("read_only", "low_risk_write"))
            self.assertIn(name, self.app.tools.allowlist)

    def test_phase5_write_tool_goes_through_approval_gate_and_audit(self):
        params = {
            "name": "approved-job",
            "when": "2030-01-01T00:00:00+00:00",
            "payload": {"kind": "notification", "message": "hello"},
        }
        pending = self.app.run_tool("automation.schedule", params)
        self.assertEqual(pending.status, "pending_approval")
        self.assertTrue(pending.token_id)
        token = self.app.approvals.approve(pending.token_id, self.app.user)
        approved = self.app.run_tool("automation.schedule", params, approval_token=token)
        self.assertEqual(approved.status, "ok")
        events = self.app.history(tool="automation.schedule")
        self.assertTrue(any(event.get("status") == "pending_approval" for event in events))
        self.assertTrue(any(event.get("status") == "ok" for event in events))

    def test_dry_run_of_phase5_tool_has_no_side_effects(self):
        before = len(self.app.scheduler.list_jobs())
        result = self.app.run_tool(
            "automation.schedule",
            {"name": "dry", "when": "2030-01-01T00:00:00+00:00"},
            dry_run=True,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(before, len(self.app.scheduler.list_jobs()))
        self.assertTrue(result.as_dict()["dry_run"])

    def test_always_blocked_and_kill_switch_apply_to_phase5_tools(self):
        blocked = self.app.run_tool("notify.send", {"message": "run payment execution"})
        self.assertEqual(blocked.status, "blocked")
        self.assertTrue(any(event.get("decision") == "blocked" for event in self.app.history(tool="notify.send")))
        self.app.kill("phase 5 test")
        killed = self.app.run_tool("brief.morning", {})
        self.assertEqual(killed.status, "blocked")

    def test_scheduler_tool_jobs_use_the_safety_layer(self):
        self.app.automation.scheduler._clock = lambda: NOW
        self.app.scheduler.schedule(
            "audited-tool-job", NOW.isoformat(),
            {"kind": "tool", "tool": "demo.echo", "params": {"text": "scheduled"}},
        )
        results = self.app.scheduler.tick(now=NOW)
        self.assertEqual(results[0]["status"], "ok")
        self.assertTrue(any(event.get("tool") == "demo.echo" for event in self.app.history()))

    def test_degradation_lower_priority_for_scheduled_jobs(self):
        target = {"value": "normal"}

        def level(snapshot=None):
            return target["value"]

        with patch.object(self.app.monitor, "level", side_effect=level):
            self.app.degradation_tick()
            target["value"] = "degrade"
            self.app.degradation_tick()
        self.assertEqual(self.app.scheduler.status()["resource_level"], "degrade")
        self.app.scheduler._clock = lambda: NOW
        self.app.scheduler.schedule("heavy", NOW.isoformat(), resource={"heavy": True})
        results = self.app.scheduler.tick(now=NOW)
        self.assertEqual(results[0]["status"], "deferred")

    def test_cli_brief_and_full_status(self):
        emitted: list[str] = []
        handle_command(self.app, "/brief evening", emitted.append)
        self.assertTrue(any("BRIEF (EVENING)" in item for item in emitted))
        self.assertTrue(any("unresolved_decisions" in item for item in emitted))
        handle_command(self.app, "/status", emitted.append)
        rendered = emitted[-1]
        for token in ("STATUS", "tasks:", "alerts:", "approvals:", "health:", "projects:", "memory:", "automation:"):
            self.assertIn(token, rendered)

    def test_briefs_end_to_end_without_voice_or_cloud(self):
        self.app.config.sections["voice"]["enabled"] = False
        morning = self.app.brief("morning")
        evening = self.app.brief("evening")
        self.assertEqual(morning["kind"], "morning")
        self.assertEqual(evening["kind"], "evening")
        for name, section in morning["sections"].items():
            self.assertTrue("count" in section or name == "weather")
        self.assertEqual(evening["sections"]["open_risks"]["count"], 0)

    def test_automation_disabled_keeps_earlier_stubs(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.sections["automation"]["enabled"] = False
        app = OmenApp(cfg, root=self.root / "disabled", output=lambda _text: None)
        try:
            self.assertIsNone(app.automation)
            self.assertEqual(app.scheduler.status()["implementation"], "stub-no-op")
            self.assertEqual(app.notifier.status()["implementation"], "stub-no-op")
            self.assertEqual(app.brief()["note"], "automation disabled; stub-grade brief from audit history")
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
