"""Linux global push-to-talk hotkey backed by evdev.

The hotkey may be a single key (``KEY_RIGHTCTRL``) or a chord such as
``CTRL+V``. Modifiers are tracked across every readable keyboard device so a
chord works even when the modifier and the main key arrive from different
devices.
"""

from __future__ import annotations

import threading
import selectors
import queue
from typing import Callable


class HotkeyUnavailable(RuntimeError):
    pass


_MODIFIER_ALIASES: dict[str, tuple[str, ...]] = {
    "ctrl": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "control": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "alt": ("KEY_LEFTALT", "KEY_RIGHTALT"),
    "shift": ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"),
    "super": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    "meta": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
}


def parse_hotkey_spec(spec: str) -> tuple[list[str], str]:
    """Split ``CTRL+V`` into ``(['CTRL'], 'V')``; single keys stay unchanged."""
    parts = [part.strip() for part in str(spec).split("+") if part.strip()]
    if not parts:
        raise ValueError("empty hotkey specification")
    return parts[:-1], parts[-1]


class ChordTracker:
    """Pure state machine for a main key plus optional modifier keys."""

    def __init__(self, main_code: int, modifier_codes: set[int]) -> None:
        self.main_code = main_code
        self.modifier_codes = modifier_codes
        self.active = False
        self.held: set[int] = set()

    def handle(self, event_type: int, code: int, value: int, ev_key: int) -> str | None:
        """Return ``'press'``, ``'release'`` or ``None`` for one evdev event."""
        if event_type != ev_key:
            return None
        if code == self.main_code:
            if value == 1:
                if not self.modifier_codes or (self.held & self.modifier_codes):
                    self.active = True
                    return "press"
            elif value == 0 and self.active:
                self.active = False
                return "release"
            return None
        if code in self.modifier_codes:
            if value == 1:
                self.held.add(code)
            elif value == 0:
                self.held.discard(code)
                if self.active and not (self.held & self.modifier_codes):
                    self.active = False
                    return "release"
        return None


class EvdevPushToTalk:
    def __init__(self, key_name: str, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        self.key_name = key_name
        self.on_press = on_press
        self.on_release = on_release
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self._ready = threading.Event()
        self._callbacks: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._callback_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._callback_thread = threading.Thread(target=self._run_callbacks, name="omen-voice-events", daemon=True)
        self._callback_thread.start()
        self._thread = threading.Thread(target=self._run, name="omen-voice-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def _run(self) -> None:
        try:
            from evdev import InputDevice, ecodes, list_devices
        except ImportError:
            self.error = "BLOCKED-EXTERNAL: install the pinned 'voice' extra for global hotkeys"
            self._ready.set()
            return
        try:
            modifier_names, main_name = parse_hotkey_spec(self.key_name)
        except ValueError as exc:
            self.error = f"Invalid hotkey specification {self.key_name!r}: {exc}"
            self._ready.set()
            return
        main_code = ecodes.ecodes.get(main_name)
        if main_code is None and not main_name.startswith("KEY_"):
            main_code = ecodes.ecodes.get(f"KEY_{main_name}")
        if main_code is None:
            self.error = f"Unknown evdev key name {main_name!r}"
            self._ready.set()
            return
        modifier_codes: set[int] = set()
        for name in modifier_names:
            aliases = _MODIFIER_ALIASES.get(name.lower(), (name,))
            for alias in aliases:
                code = ecodes.ecodes.get(alias)
                if code is not None:
                    modifier_codes.add(code)
        if modifier_names and not modifier_codes:
            self.error = f"Unknown evdev modifier(s) in hotkey {self.key_name!r}"
            self._ready.set()
            return
        wanted = modifier_codes | {main_code}
        devices = []
        for path in list_devices():
            try:
                device = InputDevice(path)
                if wanted & set(device.capabilities().get(ecodes.EV_KEY, [])):
                    devices.append(device)
            except OSError:
                continue
        if not devices:
            self.error = "BLOCKED-EXTERNAL: no readable keyboard device; add the user to the input group or configure a permitted hotkey provider"
            self._ready.set()
            return
        tracker = ChordTracker(main_code, modifier_codes)
        for device in devices:
            try:
                tracker.held.update(code for code in device.active_keys() if code in modifier_codes)
            except (OSError, ValueError):
                continue
        try:
            selector = selectors.DefaultSelector()
            for device in devices:
                selector.register(device, selectors.EVENT_READ)
            self._ready.set()
            while not self._stop.is_set():
                for key, _ in selector.select(timeout=0.25):
                    device = key.fileobj
                    try:
                        for event in device.read():
                            action = tracker.handle(event.type, event.code, event.value, ecodes.EV_KEY)
                            if action == "press":
                                self._callbacks.put(self.on_press)
                            elif action == "release":
                                self._callbacks.put(self.on_release)
                    except OSError:
                        continue
        except Exception as exc:
            self.error = f"Global hotkey listener failed: {type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            if "selector" in locals():
                selector.close()
            for device in devices:
                device.close()

    def _run_callbacks(self) -> None:
        while not self._stop.is_set():
            try:
                callback = self._callbacks.get(timeout=0.25)
            except queue.Empty:
                continue
            if callback is None:
                return
            try:
                callback()
            except Exception as exc:
                self.error = f"Global hotkey action failed: {type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self._stop.set()
        self._callbacks.put(None)
