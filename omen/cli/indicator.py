"""Terminal listening indicator with a Siri-style animated waveform."""

from __future__ import annotations

import sys
import threading
from typing import TextIO


class ListeningIndicator:
    """Draws an animated, colorful waveform while Omen is capturing voice."""

    _WAVE = "▁▂▃▄▅▆▇█▇▆▅▄▃▂"
    _COLORS = ("\033[95m", "\033[96m", "\033[94m", "\033[93m", "\033[92m")
    _BARS = 7

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active = False
        self._title = "LISTENING"

    def start(self, title: str = "LISTENING") -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self._title = title
            self._stop.clear()
            self._stream.write("\n")
            self._stream.flush()
            self._thread = threading.Thread(target=self._run, name="omen-voice-indicator", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        index = 0
        while not self._stop.is_set():
            bars = "".join(self._WAVE[(index + offset) % len(self._WAVE)] for offset in range(self._BARS))
            color = self._COLORS[(index // 2) % len(self._COLORS)]
            frame = f"\r\033[2K  {color}◉\033[0m  \033[1m{self._title}\033[0m  {color}{bars}\033[0m  "
            self._stream.write(frame)
            self._stream.flush()
            index += 1
            self._stop.wait(0.09)

    def stop(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._stream.write("\r\033[2K\n")
        self._stream.flush()
        with self._lock:
            self._active = False
            self._thread = None
