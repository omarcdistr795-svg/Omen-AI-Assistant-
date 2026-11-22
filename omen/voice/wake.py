"""Optional local wake phrase detector, using the configured Vosk model."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable
from dataclasses import dataclass

from .audio import AudioDevice
from .pipeline import VoiceBackendUnavailable


class WakePhraseGate:
    """Exact normalized phrase match plus a configurable confidence floor."""

    def __init__(self, phrase: str, sensitivity: float = 0.65) -> None:
        phrase = " ".join(re.findall(r"[a-z0-9']+", phrase.lower()))
        if not phrase:
            raise ValueError("Wake phrase cannot be empty")
        if not 0.0 <= sensitivity <= 1.0:
            raise ValueError("Wake sensitivity must be between 0 and 1")
        self.phrase = phrase
        self.sensitivity = sensitivity

    def matches(self, transcript: str, confidence: float) -> bool:
        normalized = " ".join(re.findall(r"[a-z0-9']+", transcript.lower()))
        return normalized == self.phrase and float(confidence) >= self.sensitivity


@dataclass(frozen=True)
class IdleFalseTriggerMeasurement:
    duration_seconds: float
    windows: int
    false_triggers: int
    false_triggers_per_hour: float


def measure_idle_false_triggers(
    gate: WakePhraseGate,
    recognizer_outputs: list[tuple[str, float]],
    *,
    duration_seconds: float,
) -> IdleFalseTriggerMeasurement:
    """Measure false activations on a labelled idle recognizer-output fixture."""
    if duration_seconds <= 0:
        raise ValueError("Idle test duration must be positive")
    false_triggers = sum(1 for transcript, confidence in recognizer_outputs if gate.matches(transcript, confidence))
    rate = false_triggers * 3600 / duration_seconds
    return IdleFalseTriggerMeasurement(duration_seconds, len(recognizer_outputs), false_triggers, rate)


class VoskWakeWordDetector:
    """Continuous local keyword recognition; model is released when run exits."""

    def __init__(self, audio: AudioDevice, model_path: Path | str, phrase: str, sensitivity: float) -> None:
        self.audio = audio
        self.model_path = Path(model_path)
        self.gate = WakePhraseGate(phrase, sensitivity)
        self.detections = 0

    def run(self, stop_event: threading.Event, on_wake: Callable[[str], None]) -> None:
        if not self.model_path.is_dir():
            raise VoiceBackendUnavailable(f"BLOCKED-EXTERNAL: local Vosk model directory is missing: {self.model_path}")
        try:
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise VoiceBackendUnavailable("BLOCKED-EXTERNAL: install the pinned voice extra for local wake detection") from exc
        model = Model(str(self.model_path))
        grammar = json.dumps([self.gate.phrase, "[unk]"])
        recognizer = KaldiRecognizer(model, int(getattr(self.audio, "sample_rate", 16000)), grammar)
        recognizer.SetWords(True)
        for frame in self.audio.audio_frames(stop_event):
            if stop_event.is_set():
                break
            if not recognizer.AcceptWaveform(frame):
                continue
            result: dict[str, Any] = json.loads(recognizer.Result())
            transcript = str(result.get("text", ""))
            words = result.get("result") or []
            confidence = sum(float(word.get("conf", 0.0)) for word in words) / len(words) if words else 0.0
            if self.gate.matches(transcript, confidence):
                self.detections += 1
                on_wake(transcript)
                return


class MockWakeWordDetector:
    """Recorded or synthetic recognizer outputs for deterministic tests."""

    def __init__(self, outputs: list[tuple[str, float]], phrase: str = "Hey Omen", sensitivity: float = 0.65) -> None:
        self.outputs = list(outputs)
        self.gate = WakePhraseGate(phrase, sensitivity)
        self.detections = 0

    def run(self, stop_event: threading.Event, on_wake: Callable[[str], None]) -> None:
        for transcript, confidence in self.outputs:
            if stop_event.is_set():
                return
            if self.gate.matches(transcript, confidence):
                self.detections += 1
                on_wake(transcript)
                return
