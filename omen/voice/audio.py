"""Local audio devices. Optional OS access is deferred until recording/playback."""

from __future__ import annotations

import shutil
import signal
import selectors
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol
from collections.abc import Iterator


class AudioDevice(Protocol):
    def begin_recording(self) -> Path: ...
    def end_recording(self) -> Path: ...
    def play_file(self, path: Path) -> dict: ...
    def interrupt_playback(self) -> dict: ...
    def audio_frames(self, stop_event: threading.Event) -> Iterator[bytes]: ...
    def stop(self) -> dict: ...


class AudioUnavailable(RuntimeError):
    """A local audio executable or device is unavailable."""


class PipeWireAudioDevice:
    """PipeWire command-line audio adapter; it never routes audio over a network."""

    def __init__(
        self,
        *,
        record_command: str = "pw-record",
        playback_command: str = "pw-play",
        sample_rate: int = 16000,
        channels: int = 1,
        max_seconds: int = 20,
        record_target: str | None = None,
    ) -> None:
        if sample_rate < 8000 or channels not in (1, 2) or max_seconds <= 0:
            raise ValueError("Invalid audio capture settings")
        self.record_command = record_command
        self.playback_command = playback_command
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_seconds = max_seconds
        self.record_target = record_target
        self._recording: subprocess.Popen | None = None
        self._recording_path: Path | None = None
        self._playback: subprocess.Popen | None = None
        self._interrupted_playbacks: set[int] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _require(command: str) -> str:
        found = shutil.which(command)
        if not found:
            raise AudioUnavailable(f"BLOCKED-EXTERNAL: audio command {command!r} is not installed")
        return found

    def begin_recording(self) -> Path:
        with self._lock:
            if self._recording is not None:
                raise RuntimeError("Audio recording is already active")
            command = self._require(self.record_command)
            handle = tempfile.NamedTemporaryFile(prefix="omen-voice-", suffix=".wav", delete=False)
            path = Path(handle.name)
            handle.close()
            argv = [
                command, "--rate", str(self.sample_rate), "--channels", str(self.channels),
                "--format", "s16", "--container", "wav", str(path),
            ]
            if self.record_target:
                argv[1:1] = ["--target", self.record_target]
            try:
                self._recording = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError:
                path.unlink(missing_ok=True)
                raise
            self._recording_path = path
            return path

    def end_recording(self) -> Path:
        with self._lock:
            process, path = self._recording, self._recording_path
            if process is None or path is None:
                raise RuntimeError("No audio recording is active")
            self._recording = None
            self._recording_path = None
        process.send_signal(signal.SIGINT)
        try:
            _, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()
            path.unlink(missing_ok=True)
            raise AudioUnavailable("PipeWire recorder did not stop after push-to-talk release")
        if process.returncode not in (0, -signal.SIGINT, 128 + signal.SIGINT):
            path.unlink(missing_ok=True)
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise AudioUnavailable(f"PipeWire recording failed: {detail or process.returncode}")
        if path.stat().st_size <= 44:
            path.unlink(missing_ok=True)
            raise AudioUnavailable("No microphone samples were recorded")
        return path

    def play_file(self, path: Path) -> dict:
        command = self._require(self.playback_command)
        with self._lock:
            if self._playback is not None:
                self._stop_process(self._playback)
            self._playback = subprocess.Popen([command, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            process = self._playback
        _, stderr = process.communicate()
        with self._lock:
            interrupted = id(process) in self._interrupted_playbacks
            self._interrupted_playbacks.discard(id(process))
            if self._playback is process:
                self._playback = None
        if process.returncode and not interrupted:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise AudioUnavailable(f"PipeWire playback failed: {detail or process.returncode}")
        return {"interrupted": interrupted}

    def interrupt_playback(self) -> dict:
        started = time.perf_counter()
        with self._lock:
            process, self._playback = self._playback, None
            if process is not None and process.poll() is None:
                self._interrupted_playbacks.add(id(process))
        if process is not None:
            active = process.poll() is None
            if active:
                self._stop_process(process)
        else:
            active = False
        return {"interrupted": active, "latency_ms": (time.perf_counter() - started) * 1000}

    def audio_frames(self, stop_event: threading.Event) -> Iterator[bytes]:
        command = self._require(self.record_command)
        argv = [command, "--raw", "--format", "s16", "--rate", str(self.sample_rate), "--channels", str(self.channels)]
        if self.record_target:
            argv.extend(["--target", self.record_target])
        argv.append("-")
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while not stop_event.is_set():
                if not selector.select(timeout=0.1):
                    continue
                chunk = process.stdout.read(3200)
                if not chunk:
                    break
                yield chunk
        finally:
            selector.close()
            self._stop_process(process)
            if process.stderr:
                process.stderr.close()
            process.stdout.close()

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def stop(self) -> dict:
        stopped = []
        with self._lock:
            recording, playback = self._recording, self._playback
            path = self._recording_path
            self._recording = self._recording_path = self._playback = None
            if playback is not None and playback.poll() is None:
                self._interrupted_playbacks.add(id(playback))
        if recording is not None:
            self._stop_process(recording)
            stopped.append("recording")
            if path:
                path.unlink(missing_ok=True)
        if playback is not None:
            self._stop_process(playback)
            stopped.append("playback")
        return {"stopped": stopped}


class MockAudioDevice:
    """Test device that consumes a recorded sample and captures played output."""

    def __init__(self, sample_path: Path | str | None = None, *, block_playback: bool = False) -> None:
        self.sample_path = Path(sample_path) if sample_path else None
        self.played: list[Path] = []
        self.active = False
        self.block_playback = block_playback
        self.playing = threading.Event()
        self._release_playback = threading.Event()
        self.interrupt_latency_ms: float | None = None

    def begin_recording(self) -> Path:
        if not self.sample_path or not self.sample_path.is_file():
            raise AudioUnavailable("Mock audio device has no recorded input sample")
        self.active = True
        return self.sample_path

    def end_recording(self) -> Path:
        if not self.active or not self.sample_path:
            raise RuntimeError("Mock recording was not active")
        self.active = False
        return self.sample_path

    def play_file(self, path: Path) -> dict:
        self.played.append(Path(path))
        if self.block_playback:
            self._release_playback.clear()
            self.playing.set()
            self._release_playback.wait(timeout=5)
            self.playing.clear()
        return {"interrupted": self._release_playback.is_set()}

    def interrupt_playback(self) -> dict:
        started = time.perf_counter()
        interrupted = self.playing.is_set()
        self._release_playback.set()
        self.interrupt_latency_ms = (time.perf_counter() - started) * 1000
        return {"interrupted": interrupted, "latency_ms": self.interrupt_latency_ms}

    def audio_frames(self, stop_event: threading.Event):
        while not stop_event.wait(0.1):
            yield b"\x00" * 3200

    def stop(self) -> dict:
        self.active = False
        self.interrupt_playback()
        return {"stopped": ["mock"]}
