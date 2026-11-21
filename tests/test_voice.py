from __future__ import annotations

import tempfile
import threading
import time
import wave
from array import array
from pathlib import Path
import unittest

from omen.config import load_config
from omen.app import OmenApp
from omen.stubs.memory import InMemoryMemoryStore
from omen.voice.audio import MockAudioDevice
from omen.voice.pipeline import Transcription, VoiceBackendUnavailable
from omen.voice.service import VoiceService
from omen.voice.wake import MockWakeWordDetector, WakePhraseGate, measure_idle_false_triggers


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(dict(event))


class FakeRecognizer:
    def __init__(self, text="hello Omen", confidence=0.99):
        self.result = Transcription(text, confidence)

    def transcribe(self, _path):
        return self.result


class FakeSynthesizer:
    def __init__(self):
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        handle = tempfile.NamedTemporaryFile(prefix="omen-test-tts-", suffix=".wav", delete=False)
        handle.write(b"wav")
        handle.close()
        return Path(handle.name)


class FakeVAD:
    def __init__(self, voiced=True):
        self.voiced = voiced

    def analyze(self, _path):
        return type("Result", (), {"voiced": self.voiced, "duration_seconds": 0.5, "voiced_ratio": 0.5})()


class BlockingWakeDetector:
    def __init__(self):
        self.started = threading.Event()

    def run(self, stop_event, _on_wake):
        self.started.set()
        stop_event.wait(timeout=5)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = self.root / "recorded-command.wav"
        with wave.open(str(self.sample), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(array("h", [1200] * 8000).tobytes())
        self.audio = MockAudioDevice(self.sample)
        self.recognizer = FakeRecognizer()
        self.synthesizer = FakeSynthesizer()
        self.audit = FakeAudit()
        self.messages = []
        self.app_config = load_config()
        self.app_config.sections["voice"]["follow_up_enabled"] = False
        self.voice = VoiceService(
            self.app_config,
            root=self.root,
            audit=self.audit,
            output=self.messages.append,
            audio_device=self.audio,
            recognizer=self.recognizer,
            synthesizer=self.synthesizer,
            vad=FakeVAD(),
        )
        self.brain_calls = []
        self.kills = []
        self.voice.bind(brain=lambda text: self.brain_calls.append(text) or "Understood.", kill=lambda reason: self.kills.append(reason) or {"active": True})

    def tearDown(self):
        self.voice.stop()
        self.temp.cleanup()

    def test_recorded_sample_pipeline_routes_through_brain_and_speaks(self):
        started = time.perf_counter()
        result = self.voice.process_sample(self.sample)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertTrue(result["accepted"])
        self.assertEqual(self.brain_calls, ["hello Omen"])
        self.assertEqual(self.synthesizer.calls, ["Understood."])
        self.assertEqual(len(self.audio.played), 1)
        self.assertLess(elapsed_ms, 1500)
        self.assertTrue(any("Heard: hello Omen" in item for item in self.messages))
        self.assertTrue(any(e["event_type"] == "voice_input" for e in self.audit.events))
        self.assertNotIn("hello Omen", str(self.audit.events))

    def test_risky_and_uncertain_transcripts_require_confirmation(self):
        self.recognizer.result = Transcription("delete the old note", 0.95)
        self.voice.confirmation = lambda transcript, _confidence: transcript == "delete the old note"
        result = self.voice.process_sample(self.sample)
        self.assertTrue(result["accepted"])
        self.assertEqual(self.brain_calls, ["delete the old note"])

        self.recognizer.result = Transcription("run shell command", 0.95)
        self.voice.confirmation = lambda _transcript, _confidence: False
        result = self.voice.process_sample(self.sample)
        self.assertEqual(result["reason"], "transcript_not_confirmed")
        self.assertEqual(len(self.brain_calls), 1)

        self.recognizer.result = Transcription("maybe", 0.4)
        self.voice.confirmation = lambda _transcript, _confidence: True
        self.assertTrue(self.voice.process_sample(self.sample)["accepted"])

    def test_spoken_kill_uses_existing_kill_switch_and_skips_brain(self):
        self.recognizer.result = Transcription("Omen, stop", 0.99)
        result = self.voice.process_sample(self.sample)
        self.assertEqual(result["action"], "kill")
        self.assertEqual(self.kills, ["spoken kill command"])
        self.assertEqual(self.brain_calls, [])

    def test_barge_in_interrupts_speech_under_300ms(self):
        audio = MockAudioDevice(self.sample, block_playback=True)
        self.voice.audio = audio
        speaker = threading.Thread(target=lambda: self.voice.speak("A measured reply."), daemon=True)
        speaker.start()
        self.assertTrue(audio.playing.wait(timeout=1))
        started = time.perf_counter()
        self.voice.begin_push_to_talk()
        latency_ms = (time.perf_counter() - started) * 1000
        speaker.join(timeout=1)
        self.assertFalse(speaker.is_alive())
        self.assertLess(latency_ms, 300)
        self.assertLess(float(self.voice.status()["last_barge_in_ms"]), 300)
        self.assertFalse(audio.playing.is_set())
        self.voice.stop()

    def test_speaker_echo_guard_suppresses_omen_reply(self):
        self.app_config.sections["voice"]["audio_mode"] = "speaker"
        reply = "The local status is ready and no action is pending."
        self.voice.speak(reply)
        self.recognizer.result = Transcription(reply, 0.99)
        result = self.voice.process_sample(self.sample)
        self.assertEqual(result["reason"], "speaker_echo_suppressed")
        self.assertEqual(self.brain_calls, [])

    def test_wake_phrase_and_sensitivity_are_configurable(self):
        gate = WakePhraseGate("Computer, Omen", 0.7)
        self.assertTrue(gate.matches("Computer Omen", 0.71))
        self.assertFalse(gate.matches("Computer Omen", 0.69))
        self.assertFalse(gate.matches("Computer, oh man", 0.99))

    def test_idle_false_trigger_rate_is_measured(self):
        gate = WakePhraseGate("Hey Omen", 0.7)
        idle_outputs = [("", 0.0), ("fan noise", 0.99), ("hey oh man", 0.95), ("hey omen", 0.6)] * 150
        measurement = measure_idle_false_triggers(gate, idle_outputs, duration_seconds=600)
        self.assertEqual(measurement.windows, 600)
        self.assertEqual(measurement.false_triggers, 0)
        self.assertEqual(measurement.false_triggers_per_hour, 0.0)

    def test_wake_word_suspends_under_pressure_and_recovers(self):
        self.app_config.sections["voice"]["wake"]["enabled"] = True
        detector = BlockingWakeDetector()
        self.voice.wake_detector = detector
        started = self.voice.start_wake_listener()
        self.assertTrue(started["started"])
        self.assertTrue(detector.started.wait(timeout=1))
        self.voice.apply_degradation("degrade")
        self.assertTrue(self.voice.status()["wake_word"]["suspended_for_resources"])
        self.voice.apply_degradation("normal")
        self.assertFalse(self.voice.status()["wake_word"]["suspended_for_resources"])

    def test_wake_phrase_starts_a_follow_up_capture_window(self):
        self.app_config.sections["voice"]["wake"]["enabled"] = True
        self.app_config.sections["voice"]["wake"]["phrase"] = "Hey Omen"
        self.voice.wake_detector = MockWakeWordDetector([("fan noise", 0.99), ("hey omen", 0.9)])
        self.voice.start_wake_listener()
        limit = time.monotonic() + 1.0
        while not self.voice.status()["follow_up_window_active"] and time.monotonic() < limit:
            time.sleep(0.01)
        self.assertTrue(self.voice.status()["follow_up_window_active"])
        self.assertEqual(self.voice.status()["wake_word"]["detections"], 1)

    def test_follow_up_window_runs_after_a_response_when_enabled(self):
        self.app_config.sections["voice"]["follow_up_enabled"] = True
        self.voice._arm_follow_up_window()
        self.assertTrue(self.voice.status()["follow_up_window_active"])
        self.assertEqual(self.voice.status()["wake_word"]["follow_up_seconds"], 8)

    def test_app_voice_uses_existing_kill_switch_and_resource_degradation(self):
        app = OmenApp(config=self.app_config, root=self.root, memory=InMemoryMemoryStore())
        try:
            self.assertIs(app.voice.brain.__self__, app)
            self.assertIs(app.voice.kill.__self__, app)
            app.voice.audio = self.audio
            app.voice.recognizer = FakeRecognizer("Omen, stop", 0.99)
            app.voice.vad = FakeVAD()
            result = app.voice.process_sample(self.sample)
            self.assertEqual(result["action"], "kill")
            self.assertTrue(app.kill_switch.status()["active"])

            app.kill_switch.release(approver="test")
            app.monitor.level = lambda: "degrade"
            app.monitor.status = lambda: {"level": "degrade", "ram_percent": 86}
            app.degradation_tick()
            self.assertEqual(app.voice.status()["degradation"], "text_only")
            self.assertTrue(app.voice.status()["push_to_talk_fallback"])
        finally:
            app.close()

    def test_vad_rejects_silence_without_transcription(self):
        self.voice.vad = FakeVAD(voiced=False)
        result = self.voice.process_sample(self.sample)
        self.assertEqual(result["reason"], "no_speech")
        self.assertEqual(self.brain_calls, [])

    def test_degradation_switches_to_text_only_and_keeps_push_to_talk(self):
        actions = self.voice.apply_degradation("degrade")
        self.assertEqual(actions[0]["component"], "text_to_speech")
        self.assertEqual(self.voice.speak("reply")["spoken"], False)
        self.assertTrue(self.voice.status()["push_to_talk_fallback"])
        self.assertEqual(self.synthesizer.calls, [])
        self.assertTrue(self.voice.apply_degradation("normal"))

    def test_degradation_uses_lighter_voice_before_text_only(self):
        light = FakeSynthesizer()
        self.voice.light_synthesizer = light
        actions = self.voice.apply_degradation("degrade")
        self.assertIn("lighter", actions[0]["action"])
        self.assertEqual(self.voice.status()["degradation"], "light_voice")
        self.assertTrue(self.voice.speak("reply")["spoken"])
        self.assertEqual(light.calls, ["reply"])
        self.voice.apply_degradation("emergency")
        self.assertEqual(self.voice.status()["degradation"], "text_only")
        self.assertFalse(self.voice.speak("urgent reply")["spoken"])

    def test_real_backend_reports_missing_local_model_as_blocked_external(self):
        model = self.root / "missing-vosk-model"
        from omen.voice.pipeline import VoskRecognizer

        with self.assertRaisesRegex(VoiceBackendUnavailable, "BLOCKED-EXTERNAL"):
            VoskRecognizer(model).transcribe(self.sample)

    def test_speaker_identification_is_explicitly_deferred(self):
        status = self.voice.status()["speaker_identification"]
        self.assertFalse(status["enabled"])
        self.assertEqual(status["status"], "DEFERRED-UNMEASURED-BUDGET")
        self.assertIn("memory budget", status["reason"])


if __name__ == "__main__":
    unittest.main()
