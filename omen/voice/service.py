"""Push-to-talk voice loop, routed through Omen's existing chat and kill switch."""

from __future__ import annotations

import re
import importlib.util
import shutil
import time
from difflib import SequenceMatcher
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .audio import AudioDevice, AudioUnavailable, PipeWireAudioDevice
from .hotkey import EvdevPushToTalk
from .pipeline import EnergyVAD, PiperSynthesizer, Transcription, VoiceBackendUnavailable, VoskRecognizer
from .wake import VoskWakeWordDetector

RISKY_OR_AMBIGUOUS = re.compile(
    r"\b(delete|remove|send|share|publish|post|email|pay|purchase|transfer|shell|terminal|command|run|execute|install|uninstall|commit|merge|push|pull|write|update|change|restart|reboot|shutdown|format|reset|open app|system|calendar|reminder|task|note|file|kill|stop)\b",
    re.IGNORECASE,
)
KILL_PHRASE = re.compile(r"^(?:omen[, ]+)?(?:kill(?: switch)?|stop|abort)(?:[, ]+omen)?[.! ]*$", re.IGNORECASE)
AMBIGUOUS = re.compile(r"\b(it|that|there|them|the usual|the other one|do it|handle it|same as before)\b", re.IGNORECASE)


class VoiceService:
    """Replaceable speech layers, loaded only while a request is being handled."""

    phase = 4
    implementation = "local-push-to-talk"

    def __init__(
        self,
        config: Any,
        *,
        root: Path | str,
        audit: Any,
        output: Callable[[str], None] | None = None,
        audio_device: AudioDevice | None = None,
        recognizer: Any | None = None,
        synthesizer: Any | None = None,
        light_synthesizer: Any | None = None,
        vad: Any | None = None,
        confirmation: Callable[[str, float], bool] | None = None,
        resource_monitor: Any | None = None,
        wake_detector: Any | None = None,
    ) -> None:
        self.config = config
        self.root = Path(root).resolve()
        self.audit = audit
        self.output = output or (lambda _message: None)
        self.audio = audio_device
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.light_synthesizer = light_synthesizer
        self.vad = vad
        self.confirmation = confirmation
        self.resource_monitor = resource_monitor
        self.wake_detector = wake_detector
        self.brain: Callable[[str], str] | None = None
        self.kill: Callable[[str], dict] | None = None
        self.on_capture_start: Callable[[], None] | None = None
        self.on_capture_end: Callable[[], None] | None = None
        self.hotkey: EvdevPushToTalk | None = None
        self._wake_requested = False
        self._wake_suspended = False
        self._wake_stop = threading.Event()
        self._wake_thread: threading.Thread | None = None
        self._wake_error: str | None = None
        self._wake_detections = 0
        self._capture_reason = "push_to_talk"
        self._resource_stop = threading.Event()
        self._resource_thread: threading.Thread | None = None
        self._recording = False
        self._lock = threading.RLock()
        self._recording_timer: threading.Timer | None = None
        self._counts = {"transcriptions": 0, "spoken": 0, "stops": 0}
        self._last_error: str | None = None
        self._last_latency_ms: float | None = None
        self._last_barge_in_ms: float | None = None
        self._last_spoken_text = ""
        self._last_spoken_at = 0.0
        self._resource_level = "normal"
        self._degradation_mode = "normal"

    def bind(self, *, brain: Callable[[str], str], kill: Callable[[str], dict]) -> None:
        self.brain, self.kill = brain, kill

    def _value(self, path: str, default: Any = None) -> Any:
        return self.config.get(f"voice.{path}", default)

    def voice_only_answers(self) -> bool:
        """When enabled, replies are spoken and never printed as text."""
        return bool(self._value("voice_only_answers", False))

    def _path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.root / path)

    def _audio_device(self) -> AudioDevice:
        if self.audio is None:
            backend = self._value("audio_backend", "pipewire")
            if backend != "pipewire":
                raise AudioUnavailable(f"Unsupported audio backend: {backend}")
            self.audio = PipeWireAudioDevice(
                record_command=self._value("record_command", "pw-record"),
                playback_command=self._value("playback_command", "pw-play"),
                sample_rate=int(self._value("sample_rate", 16000)),
                channels=int(self._value("channels", 1)),
                max_seconds=int(self._value("max_recording_seconds", 20)),
                record_target=self._value("record_target"),
            )
        return self.audio

    def _recognizer(self) -> Any:
        if self.recognizer is None:
            backend = self._value("stt.backend", "vosk")
            if backend != "vosk":
                raise VoiceBackendUnavailable(f"Unsupported local STT backend: {backend}")
            self.recognizer = VoskRecognizer(self._path(self._value("stt.model_path", "")))
        return self.recognizer

    def _synthesizer(self) -> Any:
        if self.synthesizer is None:
            backend = self._value("tts.backend", "piper")
            if backend != "piper":
                raise VoiceBackendUnavailable(f"Unsupported local TTS backend: {backend}")
            config_path = self._value("tts.config_path")
            self.synthesizer = PiperSynthesizer(
                self._path(self._value("tts.model_path", "")),
                config_path=self._path(config_path) if config_path else None,
                length_scale=float(self._value("tts.length_scale", 1.12)),
            )
        if self._degradation_mode == "light_voice":
            light_path = self._path(self._value("tts.light_model_path", ""))
            if self.light_synthesizer is None:
                config_path = self._value("tts.light_config_path")
                self.light_synthesizer = PiperSynthesizer(
                    light_path,
                    config_path=self._path(config_path) if config_path else None,
                    length_scale=float(self._value("tts.length_scale", 1.12)),
                )
            return self.light_synthesizer
        return self.synthesizer

    def _vad(self) -> Any:
        if self.vad is None:
            if not self._value("vad.enabled", True):
                return None
            backend = self._value("vad.backend", "energy")
            if backend != "energy":
                raise VoiceBackendUnavailable(f"Unsupported VAD backend: {backend}")
            self.vad = EnergyVAD(threshold=int(self._value("vad.threshold", 450)))
        return self.vad

    def begin_push_to_talk(self) -> None:
        if self._recording:
            self._cancel_capture()
        self._begin_capture(
            int(self._value("max_recording_seconds", 300)),
            "push_to_talk",
            "Listening. Release the push-to-talk key when you are done.",
        )

    def _cancel_capture(self) -> None:
        """Discard an active capture without processing it (e.g. a stale follow-up)."""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            timer, self._recording_timer = self._recording_timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        if self.on_capture_end:
            self.on_capture_end()
        try:
            path = self._audio_device().end_recording()
            if not bool(self._value("retain_audio", False)):
                path.unlink(missing_ok=True)
        except Exception:
            pass

    def _begin_capture(self, seconds: int, reason: str, announcement: str) -> None:
        if seconds <= 0 or seconds > 600:
            raise ValueError("Voice capture duration must be between 1 and 600 seconds")
        audio = self._audio_device()
        interruption = audio.interrupt_playback() if hasattr(audio, "interrupt_playback") else {"interrupted": False, "latency_ms": 0.0}
        self._last_barge_in_ms = float(interruption.get("latency_ms", 0.0)) if interruption.get("interrupted") else None
        if self._last_barge_in_ms is not None and self._last_barge_in_ms >= 300:
            self.output(f"Barge-in took {self._last_barge_in_ms:.0f} ms; the 300 ms target was missed.")
        self._stop_wake_listener()
        if interruption.get("interrupted") and self._value("audio_mode", "headset") == "speaker":
            time.sleep(float(self._value("speaker_echo_guard_seconds", 0.15)))
        with self._lock:
            if self._recording:
                return
            audio.begin_recording()
            self._recording = True
            self._capture_reason = reason
            self._recording_timer = threading.Timer(seconds, self._on_hotkey_release)
            self._recording_timer.daemon = True
            self._recording_timer.start()
        self.output(announcement)
        if self.on_capture_start:
            self.on_capture_start()

    def end_push_to_talk(self) -> dict:
        with self._lock:
            if not self._recording:
                return {"accepted": False, "reason": "not_recording"}
            self._recording = False
            capture_reason = self._capture_reason
            timer, self._recording_timer = self._recording_timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        if self.on_capture_end:
            self.on_capture_end()
        path = self._audio_device().end_recording()
        try:
            result = self.process_sample(path)
            result["capture_reason"] = capture_reason
            if not result.get("accepted"):
                self._resume_wake_listener()
            return result
        finally:
            if not bool(self._value("retain_audio", False)):
                path.unlink(missing_ok=True)

    def process_sample(self, path: Path | str) -> dict:
        """Run a recorded sample through VAD, local STT, confirmation and Omen."""
        import time

        sample = Path(path)
        started = time.perf_counter()
        try:
            self._sync_resources()
            vad = self._vad()
            vad_result = vad.analyze(sample) if vad is not None else None
            if vad_result is not None and not vad_result.voiced:
                self._audit("voice_input", "empty", "no voiced audio detected")
                return {"accepted": False, "reason": "no_speech", "vad": vad_result.__dict__}
            result = self._recognizer().transcribe(sample)
            if isinstance(result, str):
                result = Transcription(result, 1.0)
            transcript = str(result.text).strip()
            confidence = float(result.confidence)
            self._counts["transcriptions"] += 1
            if not transcript:
                self._audit("voice_input", "empty", "speech recognizer returned no text")
                return {"accepted": False, "reason": "empty_transcript", "vad": vad_result.__dict__ if vad_result else None}
            self._last_latency_ms = (time.perf_counter() - started) * 1000
            if self._looks_like_speaker_echo(transcript):
                self._audit("voice_echo_guard", "blocked", "speaker echo matched recent Omen speech; transcript omitted")
                self.output("Echo suppressed. Hold the push-to-talk key and speak again.")
                return {"accepted": False, "reason": "speaker_echo_suppressed", "confidence": confidence}
            self.output(f"Heard: {transcript}")
            if KILL_PHRASE.fullmatch(transcript):
                if not self.kill:
                    raise RuntimeError("Voice kill switch is not bound to OmenApp")
                self.kill("spoken kill command")
                return {"accepted": True, "action": "kill", "latency_ms": self._last_latency_ms}
            needs_confirmation = bool(RISKY_OR_AMBIGUOUS.search(transcript) or AMBIGUOUS.search(transcript)) or confidence < 0.78
            if needs_confirmation and bool(self._value("require_transcript_confirmation_for_risky_or_ambiguous", True)):
                if self.confirmation is None or not self.confirmation(transcript, confidence):
                    self._audit("voice_confirmation", "denied", "transcript was not confirmed; transcript omitted")
                    return {"accepted": False, "reason": "transcript_not_confirmed", "confidence": confidence}
            if not self.brain:
                raise RuntimeError("Voice brain is not bound to OmenApp")
            reply = str(self.brain(transcript))
            if not self.voice_only_answers():
                self.output(f"Omen: {reply}")
            self._audit("voice_input", "accepted", "local transcript routed through the existing Omen brain loop")
            if reply:
                try:
                    self.speak(reply)
                except VoiceBackendUnavailable as exc:
                    self._last_error = str(exc)
                    self.output(str(exc))
                self._arm_follow_up_window()
            return {"accepted": True, "reply_length": len(reply), "confidence": confidence, "latency_ms": self._last_latency_ms}
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._audit("voice_input", "error", self._last_error)
            raise

    def speak(self, text: str) -> dict:
        self._sync_resources()
        if self._degradation_mode == "text_only":
            return {"spoken": False, "reason": "resource pressure; text-only voice reply"}
        audio_path = self._synthesizer().synthesize(str(text))
        try:
            self._last_spoken_text = str(text)
            result = self._audio_device().play_file(audio_path)
            self._last_spoken_at = time.monotonic()
        finally:
            audio_path.unlink(missing_ok=True)
        if result.get("interrupted"):
            return {"spoken": False, "interrupted": True, "characters": len(text)}
        self._counts["spoken"] += 1
        return {"spoken": True, "characters": len(text)}

    def _looks_like_speaker_echo(self, transcript: str) -> bool:
        if not self._value("echo_guard_enabled", True) or self._value("audio_mode", "headset") != "speaker":
            return False
        window = float(self._value("echo_window_seconds", 30))
        if not self._last_spoken_text or time.monotonic() - self._last_spoken_at > window:
            return False
        normalize = lambda value: " ".join(re.findall(r"[a-z0-9']+", value.lower()))
        heard, spoken = normalize(transcript), normalize(self._last_spoken_text)
        if not heard or not spoken:
            return False
        if heard == spoken:
            return True
        words = set(heard.split())
        spoken_words = set(spoken.split())
        overlap = len(words & spoken_words) / max(1, len(words | spoken_words))
        return len(words) >= 3 and overlap >= 0.82 and SequenceMatcher(None, heard, spoken).ratio() >= 0.82

    def apply_degradation(self, level: str) -> list[dict]:
        """Give TTS way under resource pressure while retaining PTT and safety."""
        if level not in {"normal", "warn", "degrade", "emergency"}:
            raise ValueError(f"Unknown resource level: {level}")
        previous = self._resource_level
        previous_mode = self._degradation_mode
        self._resource_level = level
        light_path = self._path(self._value("tts.light_model_path", ""))
        if level == "emergency":
            mode = "text_only"
        elif level == "degrade":
            mode = "light_voice" if light_path.is_file() or self.light_synthesizer is not None else "text_only"
        else:
            mode = "warn" if level == "warn" else "normal"
        changed = mode != self._degradation_mode
        self._degradation_mode = mode
        if not changed and level == previous:
            return []
        if level in ("degrade", "emergency"):
            if mode == "light_voice":
                actions = [{"component": "text_to_speech", "action": "use the configured lighter local voice; push-to-talk and safety remain active", "level": level}]
                self.output("Memory or CPU is high. Switching to the lighter local voice; push-to-talk and safety remain active.")
            else:
                actions = [{"component": "text_to_speech", "action": "text-only replies under resource pressure; push-to-talk and safety remain active", "level": level}]
                self.output("Voice replies are text-only while memory or CPU is under pressure. Push-to-talk and safety remain active.")
        elif previous_mode in ("text_only", "light_voice"):
            actions = [{"component": "text_to_speech", "action": "restore local spoken replies after resource pressure cleared", "level": level}]
            self.output("Resource pressure cleared. Local spoken replies are restored.")
        elif level == "warn" and previous_mode != "warn":
            actions = [{"component": "voice", "action": "resource use reached the warning threshold; voice remains enabled", "level": level}]
            self.output("Memory or CPU is nearing its limit. Voice remains enabled for now.")
        elif previous_mode == "warn" and level == "normal":
            actions = [{"component": "voice", "action": "resource use returned to normal", "level": level}]
            self.output("Memory and CPU use returned to normal.")
        else:
            actions = []
        if actions:
            self._audit("voice_degradation", "ok", actions[0]["action"])
        if level in ("degrade", "emergency") and self._recording and self._capture_reason != "push_to_talk":
            timer, self._recording_timer = self._recording_timer, None
            if timer is not None:
                timer.cancel()
            self._recording = False
            if self.audio:
                self.audio.stop()
            action = {"component": "wake_word", "action": "automatic follow-up capture stopped under resource pressure; push-to-talk remains active", "level": level}
            actions.append(action)
            self.output("Automatic follow-up listening stopped to save memory. Push-to-talk remains available.")
            self._audit("voice_degradation", "ok", action["action"])
        if level in ("degrade", "emergency") and bool(self._value("wake.enabled", False)):
            if not self._wake_suspended:
                self._wake_suspended = True
                self._stop_wake_listener()
                action = {"component": "wake_word", "action": "wake word disabled under resource pressure; push-to-talk remains active", "level": level}
                actions.append(action)
                self.output("Wake word is disabled until resource pressure clears. Push-to-talk remains active.")
                self._audit("voice_degradation", "ok", action["action"])
        elif self._wake_suspended and level in ("normal", "warn"):
            self._wake_suspended = False
            self.output("Resource pressure cleared. Wake word is available again.")
            self._audit("voice_degradation", "ok", "wake word restored after resource pressure cleared")
            self._resume_wake_listener()
        return actions

    def _sync_resources(self) -> None:
        if self.resource_monitor is not None:
            self.apply_degradation(self.resource_monitor.level())

    def _audit(self, event_type: str, status: str, summary: str) -> None:
        self.audit.append({
            "event_type": event_type,
            "tool": "voice_interface",
            "target": "local_audio",
            "decision": "allowed" if status in ("accepted", "empty") else "blocked",
            "status": status,
            "summary": summary,
        })

    def start_hotkey_listener(self) -> dict:
        if not bool(self._value("enabled", True)):
            return {"started": False, "reason": "disabled"}
        if self.hotkey is None:
            self.hotkey = EvdevPushToTalk(
                str(self._value("hotkey", "SUPER+SHIFT+V")),
                self.begin_push_to_talk,
                self._on_hotkey_release,
            )
        self.hotkey.start()
        if self._resource_thread is None or not self._resource_thread.is_alive():
            self._resource_stop.clear()
            self._resource_thread = threading.Thread(target=self._watch_resources, name="omen-voice-resources", daemon=True)
            self._resource_thread.start()
        self._sync_resources()
        if self._value("wake.enabled", False):
            self.start_wake_listener()
        return {"started": True, "hotkey": self._value("hotkey", "SUPER+SHIFT+V")}

    def start_wake_listener(self) -> dict:
        if not bool(self._value("wake.enabled", False)):
            return {"started": False, "reason": "disabled"}
        self._wake_requested = True
        if self._wake_suspended:
            return {"started": False, "reason": "resource_pressure"}
        if self._value("audio_mode", "headset") == "speaker" and self._last_spoken_at:
            remaining = float(self._value("speaker_echo_guard_seconds", 0.15)) - (time.monotonic() - self._last_spoken_at)
            if remaining > 0:
                time.sleep(remaining)
        if self._wake_thread and self._wake_thread.is_alive():
            return {"started": True, "phrase": self._value("wake.phrase", "Hey Omen")}
        if self.wake_detector is None:
            self.wake_detector = VoskWakeWordDetector(
                self._audio_device(),
                self._path(self._value("stt.model_path", "")),
                str(self._value("wake.phrase", "Hey Omen")),
                float(self._value("wake.sensitivity", 0.65)),
            )
        self._wake_stop = threading.Event()
        self._wake_error = None
        self._wake_thread = threading.Thread(target=self._run_wake_listener, name="omen-wake-word", daemon=True)
        self._wake_thread.start()
        return {"started": True, "phrase": self._value("wake.phrase", "Hey Omen")}

    def _run_wake_listener(self) -> None:
        try:
            self.wake_detector.run(self._wake_stop, self._on_wake_phrase)
        except Exception as exc:
            self._wake_error = f"{type(exc).__name__}: {exc}"
            self._last_error = self._wake_error

    def _on_wake_phrase(self, transcript: str) -> None:
        self._wake_detections += 1
        self._stop_wake_listener()
        self.output(f"Wake phrase detected: {transcript}. Speak your command.")
        seconds = int(self._value("follow_up_seconds", 8))
        def begin_capture() -> None:
            time.sleep(0.15)
            self._begin_capture(seconds, "wake_follow_up", "Listening for your command.")
        worker = threading.Thread(
            target=begin_capture,
            name="omen-wake-capture",
            daemon=True,
        )
        worker.start()

    def _arm_follow_up_window(self) -> None:
        if self._resource_level in ("degrade", "emergency"):
            return
        if not bool(self._value("follow_up_enabled", True)):
            self._resume_wake_listener()
            return
        seconds = int(self._value("follow_up_seconds", 8))
        self._stop_wake_listener()
        self._begin_capture(seconds, "follow_up", f"Follow-up window: {seconds} seconds. Speak, or hold {self._value('hotkey', 'KEY_RIGHTCTRL')} to finish sooner.")

    def _resume_wake_listener(self) -> None:
        if self._wake_requested and not self._wake_suspended and not self._recording:
            self.start_wake_listener()

    def _stop_wake_listener(self) -> None:
        self._wake_stop.set()
        thread = self._wake_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.4)
        if thread is None or not thread.is_alive():
            self._wake_thread = None

    def _watch_resources(self) -> None:
        while not self._resource_stop.wait(1.0):
            try:
                self._sync_resources()
            except Exception as exc:
                self._last_error = f"Resource monitor failed: {type(exc).__name__}: {exc}"
                self.output(self._last_error)
                return

    def _on_hotkey_release(self) -> None:
        worker = threading.Thread(target=self._process_hotkey_release, name="omen-voice-turn", daemon=True)
        worker.start()

    def _process_hotkey_release(self) -> None:
        try:
            result = self.end_push_to_talk()
            if result.get("accepted"):
                self.output(f"Voice request complete ({result.get('latency_ms', 0):.0f} ms STT).")
                self._resume_wake_listener()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.output(self._last_error)
            self._resume_wake_listener()

    def stop(self) -> dict:
        if self.hotkey:
            self.hotkey.stop()
        self._resource_stop.set()
        self._stop_wake_listener()
        timer, self._recording_timer = self._recording_timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        audio_result = self.audio.stop() if self.audio else {"stopped": []}
        if self._recording and self.on_capture_end:
            self.on_capture_end()
        self._recording = False
        self._counts["stops"] += 1
        return {"hotkey_stopped": bool(self.hotkey), "audio": audio_result}

    def status(self) -> dict:
        model_path = self._path(self._value("stt.model_path", ""))
        tts_path = self._path(self._value("tts.model_path", ""))
        light_tts_path = self._path(self._value("tts.light_model_path", ""))
        try:
            vosk_installed = importlib.util.find_spec("vosk") is not None
            evdev_installed = importlib.util.find_spec("evdev") is not None
        except (ImportError, ValueError):
            vosk_installed = evdev_installed = False
        piper_installed = shutil.which("piper") is not None
        record_installed = shutil.which(str(self._value("record_command", "pw-record"))) is not None
        playback_installed = shutil.which(str(self._value("playback_command", "pw-play"))) is not None
        ready = (
            bool(self._value("enabled", True)) and model_path.is_dir() and tts_path.is_file()
            and vosk_installed and piper_installed and evdev_installed and record_installed and playback_installed
        )
        result: dict[str, Any] = {
            "phase": self.phase,
            "implementation": self.implementation,
            "enabled": bool(self._value("enabled", True)),
            "mode": self._value("mode", "push_to_talk"),
            "hotkey": self._value("hotkey", "SUPER+SHIFT+V"),
            "ready": ready,
            "blocked_external": bool(self._value("enabled", True)) and not ready,
            "stt_model_available": model_path.is_dir(),
            "tts_model_available": tts_path.is_file(),
            "lighter_tts_model_available": light_tts_path.is_file(),
            "stt_backend_available": vosk_installed,
            "tts_backend_available": piper_installed,
            "hotkey_backend_available": evdev_installed,
            "audio_commands_available": record_installed and playback_installed,
            "transcriptions": self._counts["transcriptions"],
            "spoken": self._counts["spoken"],
            "stops": self._counts["stops"],
            "last_latency_ms": self._last_latency_ms,
            "last_barge_in_ms": self._last_barge_in_ms,
            "degradation": self._degradation_mode,
            "push_to_talk_fallback": True,
            "follow_up_window_active": self._recording and self._capture_reason in ("follow_up", "wake_follow_up"),
            "audio_mode": self._value("audio_mode", "headset"),
            "software_echo_guard": bool(self._value("echo_guard_enabled", True)),
            "os_echo_target_configured": bool(self._value("record_target")),
            "wake_word": {
                "enabled": bool(self._value("wake.enabled", False)),
                "phrase": self._value("wake.phrase", "Hey Omen"),
                "sensitivity": self._value("wake.sensitivity", 0.65),
                "active": bool(self._wake_thread and self._wake_thread.is_alive()),
                "suspended_for_resources": self._wake_suspended,
                "detections": self._wake_detections,
                "follow_up_seconds": self._value("follow_up_seconds", 8),
                "error": self._wake_error,
            },
            "speaker_identification": {
                "enabled": False,
                "status": "DEFERRED-UNMEASURED-BUDGET",
                "reason": self._value("speaker_identification.deferred_reason", "no local speaker model available to benchmark within the Omen memory budget"),
            },
        }
        if self.hotkey and self.hotkey.error:
            result["hotkey_error"] = self.hotkey.error
            result["blocked_external"] = True
        if self._last_error:
            result["last_error"] = self._last_error
        return result
