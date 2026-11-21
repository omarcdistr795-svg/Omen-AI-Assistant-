"""Hotkey chord parsing and modifier-tracking tests (no evdev required)."""

from __future__ import annotations

import unittest

from omen.voice.hotkey import ChordTracker, parse_hotkey_spec


EV_KEY = 1
CTRL_L, CTRL_R, ALT_L, ALT_R, KEY_V, KEY_A = 29, 97, 56, 100, 47, 30


class HotkeyChordTests(unittest.TestCase):
    def test_parse_single_key_and_chord(self) -> None:
        self.assertEqual(parse_hotkey_spec("KEY_RIGHTCTRL"), ([], "KEY_RIGHTCTRL"))
        self.assertEqual(parse_hotkey_spec("V"), ([], "V"))
        self.assertEqual(parse_hotkey_spec("CTRL+V"), (["CTRL"], "V"))
        self.assertEqual(parse_hotkey_spec("Shift + Alt + V"), (["Shift", "Alt"], "V"))

    def test_chord_only_fires_when_modifier_is_held(self) -> None:
        tracker = ChordTracker(KEY_V, {CTRL_L, CTRL_R})
        self.assertIsNone(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY))
        self.assertIsNone(tracker.handle(EV_KEY, KEY_V, 0, EV_KEY))
        self.assertEqual(tracker.handle(EV_KEY, CTRL_L, 1, EV_KEY), None)
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY), "press")
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 0, EV_KEY), "release")
        self.assertFalse(tracker.active)

    def test_chord_supports_either_ctrl_and_release_order(self) -> None:
        tracker = ChordTracker(KEY_V, {CTRL_L, CTRL_R})
        tracker.handle(EV_KEY, CTRL_R, 1, EV_KEY)
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY), "press")
        self.assertEqual(tracker.handle(EV_KEY, CTRL_R, 0, EV_KEY), "release")
        self.assertIsNone(tracker.handle(EV_KEY, KEY_V, 0, EV_KEY))

    def test_chord_ignores_unrelated_keys_and_non_key_events(self) -> None:
        tracker = ChordTracker(KEY_V, {CTRL_L, CTRL_R})
        tracker.handle(EV_KEY, CTRL_L, 1, EV_KEY)
        self.assertIsNone(tracker.handle(EV_KEY, KEY_A, 1, EV_KEY))
        self.assertIsNone(tracker.handle(0, KEY_V, 1, EV_KEY))
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY), "press")
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 2, EV_KEY), None)
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 0, EV_KEY), "release")

    def test_alt_modifier_chord(self) -> None:
        tracker = ChordTracker(KEY_V, {ALT_L, ALT_R})
        self.assertIsNone(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY))
        tracker.handle(EV_KEY, ALT_R, 1, EV_KEY)
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY), "press")
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 0, EV_KEY), "release")

    def test_single_key_preserves_legacy_behavior(self) -> None:
        tracker = ChordTracker(KEY_V, set())
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 1, EV_KEY), "press")
        self.assertEqual(tracker.handle(EV_KEY, KEY_V, 0, EV_KEY), "release")


if __name__ == "__main__":
    unittest.main()
