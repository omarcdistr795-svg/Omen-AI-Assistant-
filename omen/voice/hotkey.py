"""Linux global push-to-talk hotkey backed by evdev."""

from __future__ import annotations

import threading
import selectors
import queue
from typing import Callable


class HotkeyUnavailable(RuntimeError):
    pass


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
        key_code = ecodes.ecodes.get(self.key_name)
        if key_code is None:
            self.error = f"Unknown evdev key name {self.key_name!r}"
            self._ready.set()
            return
        devices = []
        for path in list_devices():
            try:
                device = InputDevice(path)
                if key_code in device.capabilities().get(ecodes.EV_KEY, []):
                    devices.append(device)
            except OSError:
                continue
        if not devices:
            self.error = "BLOCKED-EXTERNAL: no readable keyboard device; add the user to the input group or configure a permitted hotkey provider"
            self._ready.set()
            return
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
                            if event.type == ecodes.EV_KEY and event.code == key_code:
                                if event.value == 1:
                                    self._callbacks.put(self.on_press)
                                elif event.value == 0:
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
