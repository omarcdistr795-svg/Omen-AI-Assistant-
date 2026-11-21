"""Replaceable, lazy local VAD, STT and TTS adapters."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path


class VoiceBackendUnavailable(RuntimeError):
    """A local speech backend or model is missing."""


@dataclass(frozen=True)
class Transcription:
    text: str
    confidence: float


@dataclass(frozen=True)
class VadResult:
    voiced: bool
    duration_seconds: float
    voiced_ratio: float


class EnergyVAD:
    """Small dependency-free energy VAD for PCM WAV samples."""

    def __init__(self, *, threshold: int = 450, frame_ms: int = 20) -> None:
        if threshold <= 0 or frame_ms <= 0:
            raise ValueError("VAD threshold and frame size must be positive")
        self.threshold = threshold
        self.frame_ms = frame_ms

    def analyze(self, path: Path) -> VadResult:
        with wave.open(str(path), "rb") as audio:
            if audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2):
                raise ValueError("Energy VAD requires 16-bit mono or stereo PCM WAV")
            rate, channels = audio.getframerate(), audio.getnchannels()
            frames = audio.readframes(audio.getnframes())
        samples = array("h")
        samples.frombytes(frames)
        if samples.itemsize != 2:
            raise RuntimeError("Unsupported platform sample size")
        if __import__("sys").byteorder != "little":
            samples.byteswap()
        frame_samples = max(1, rate * self.frame_ms // 1000 * channels)
        active = total = 0
        for start in range(0, len(samples), frame_samples):
            block = samples[start : start + frame_samples]
            if not block:
                continue
            rms = (sum(value * value for value in block) / len(block)) ** 0.5
            active += rms >= self.threshold
            total += 1
        duration = len(samples) / (rate * channels) if rate else 0.0
        ratio = active / total if total else 0.0
        return VadResult(bool(active), duration, ratio)


class VoskRecognizer:
    """Vosk speech recognizer; imports and model loading happen per request."""

    def __init__(self, model_path: Path | str) -> None:
        self.model_path = Path(model_path)

    def transcribe(self, path: Path) -> Transcription:
        if not self.model_path.is_dir():
            raise VoiceBackendUnavailable(f"BLOCKED-EXTERNAL: local Vosk model directory is missing: {self.model_path}")
        try:
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise VoiceBackendUnavailable("BLOCKED-EXTERNAL: install the pinned 'voice' extra to enable local STT") from exc
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise ValueError("Vosk requires mono, 16-bit PCM WAV input")
            model = Model(str(self.model_path))
            recognizer = KaldiRecognizer(model, audio.getframerate())
            recognizer.SetWords(True)
            while True:
                chunk = audio.readframes(4000)
                if not chunk:
                    break
                recognizer.AcceptWaveform(chunk)
            result = json.loads(recognizer.FinalResult())
        text = str(result.get("text", "")).strip()
        words = result.get("result") or []
        confidence = sum(float(word.get("conf", 0.0)) for word in words) / len(words) if words else (1.0 if text else 0.0)
        return Transcription(text, max(0.0, min(1.0, confidence)))


class PiperSynthesizer:
    """Local Piper CLI synthesizer. The model is opened only for this request."""

    def __init__(self, model_path: Path | str, *, config_path: Path | str | None = None, length_scale: float = 1.12, command: str = "piper") -> None:
        if not 0.5 <= length_scale <= 2.0:
            raise ValueError("Piper length_scale must be between 0.5 and 2.0")
        self.model_path = Path(model_path)
        self.config_path = Path(config_path) if config_path else None
        self.length_scale = length_scale
        self.command = command

    def synthesize(self, text: str) -> Path:
        if not text.strip():
            raise ValueError("Cannot synthesize empty text")
        if not self.model_path.is_file():
            raise VoiceBackendUnavailable(f"BLOCKED-EXTERNAL: local Piper model file is missing: {self.model_path}")
        if self.config_path and not self.config_path.is_file():
            raise VoiceBackendUnavailable(f"BLOCKED-EXTERNAL: local Piper model config is missing: {self.config_path}")
        command = shutil.which(self.command)
        if not command:
            raise VoiceBackendUnavailable("BLOCKED-EXTERNAL: install the pinned 'voice' extra to enable local TTS")
        output = tempfile.NamedTemporaryFile(prefix="omen-voice-reply-", suffix=".wav", delete=False)
        output_path = Path(output.name)
        output.close()
        argv = [command, "--model", str(self.model_path), "--output_file", str(output_path), "--length_scale", str(self.length_scale)]
        if self.config_path:
            argv.extend(["--config", str(self.config_path)])
        try:
            result = subprocess.run(argv, input=text, text=True, capture_output=True, check=False)
        except OSError:
            output_path.unlink(missing_ok=True)
            raise
        if result.returncode:
            output_path.unlink(missing_ok=True)
            raise VoiceBackendUnavailable(f"Piper synthesis failed: {result.stderr.strip() or result.returncode}")
        return output_path
