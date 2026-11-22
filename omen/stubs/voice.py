"""VoiceIO stub.

Contract (Phase 4 owns the real implementation):
  listen() -> None (no microphone access)
  speak(text) -> dict with spoken=False (no audio)
  stop() -> dict with stopped count
Wake word, VAD, STT and TTS stay separate and replaceable later.
"""

from __future__ import annotations

from typing import Any


class NoOpVoiceIO:
    phase = 4
    implementation = "stub-no-op"

    def __init__(self) -> None:
        self.spoken_texts: list[str] = []
        self.stopped = 0

    def listen(self) -> dict:
        return {"heard": None, "implementation": self.implementation}

    def speak(self, text: str) -> dict:
        self.spoken_texts.append(str(text))
        return {"spoken": False, "text_length": len(str(text)), "implementation": self.implementation}

    def stop(self) -> dict:
        self.stopped += 1
        return {"stopped": self.stopped, "implementation": self.implementation}

    def status(self) -> dict:
        return {
            "phase": self.phase,
            "implementation": self.implementation,
            "spoken_texts": len(self.spoken_texts),
            "stops": self.stopped,
        }
