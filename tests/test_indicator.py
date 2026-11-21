"""Tests for the Siri-style listening indicator."""

from __future__ import annotations

import io
import threading
import time
import unittest

from omen.cli.indicator import ListeningIndicator


class RecordingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            return super().write(text)

    def getvalue(self) -> str:
        with self._lock:
            return super().getvalue()


class ListeningIndicatorTests(unittest.TestCase):
    def test_indicator_draws_and_clears_waveform(self) -> None:
        stream = RecordingStream()
        indicator = ListeningIndicator(stream)
        indicator.start()
        time.sleep(0.25)
        indicator.stop()
        output = stream.getvalue()
        self.assertIn("LISTENING", output)
        self.assertIn("◉", output)
        self.assertIn("\033[2K", output)
        self.assertIn("\033[95m", output)
        self.assertFalse(indicator._active)

    def test_start_is_idempotent_and_stop_is_safe_when_idle(self) -> None:
        stream = RecordingStream()
        indicator = ListeningIndicator(stream)
        indicator.stop()
        indicator.start()
        indicator.start()
        time.sleep(0.15)
        indicator.stop()
        self.assertEqual(stream.getvalue().count("LISTENING") >= 1, True)


if __name__ == "__main__":
    unittest.main()
