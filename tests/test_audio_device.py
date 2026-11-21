"""PipeWire recorder adapter tests."""

from __future__ import annotations

import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import MagicMock, patch

from omen.voice.audio import AudioUnavailable, PipeWireAudioDevice


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(array("h", [800] * 1600).tobytes())


class PipeWireRecorderTests(unittest.TestCase):
    def _device(self) -> PipeWireAudioDevice:
        return PipeWireAudioDevice(record_command="pw-record", playback_command="pw-play")

    def test_sigint_exit_code_one_with_audio_is_accepted(self) -> None:
        device = self._device()
        process = MagicMock()
        process.returncode = 1
        process.communicate.return_value = (b"", b"")
        with patch("omen.voice.audio.shutil.which", return_value="/usr/bin/pw-record"), patch(
            "omen.voice.audio.subprocess.Popen", return_value=process
        ):
            path = device.begin_recording()
            _write_wav(path)
            result = device.end_recording()
        self.assertEqual(result, path)
        self.assertTrue(path.exists())

    def test_unexpected_exit_code_without_audio_fails_loudly(self) -> None:
        device = self._device()
        process = MagicMock()
        process.returncode = 2
        process.communicate.return_value = (b"", b"hardware error")
        with patch("omen.voice.audio.shutil.which", return_value="/usr/bin/pw-record"), patch(
            "omen.voice.audio.subprocess.Popen", return_value=process
        ):
            path = device.begin_recording()
            _write_wav(path)
            with self.assertRaises(AudioUnavailable):
                device.end_recording()
        self.assertFalse(path.exists())

    def test_empty_recording_still_fails(self) -> None:
        device = self._device()
        process = MagicMock()
        process.returncode = 1
        process.communicate.return_value = (b"", b"")
        with patch("omen.voice.audio.shutil.which", return_value="/usr/bin/pw-record"), patch(
            "omen.voice.audio.subprocess.Popen", return_value=process
        ):
            path = device.begin_recording()
            with self.assertRaises(AudioUnavailable):
                device.end_recording()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
