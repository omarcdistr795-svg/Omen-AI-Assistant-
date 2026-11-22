"""Smoke test for the clickable voice orb module."""

from __future__ import annotations

import unittest


class VoiceIconTests(unittest.TestCase):
    def test_voice_icon_module_exposes_orb(self) -> None:
        try:
            from omen.voice_icon import VoiceOrb
        except ImportError:
            self.skipTest("GTK bindings unavailable")
        self.assertTrue(hasattr(VoiceOrb, "_on_press"))
        self.assertTrue(hasattr(VoiceOrb, "_on_release"))


if __name__ == "__main__":
    unittest.main()
