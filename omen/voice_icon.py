"""Floating push-to-talk voice orb for Omarchy/Hyprland.

Hold the mouse button on the orb to talk; release it to submit. Replies are
spoken and also shown under the orb. No keyboard hotkey is required.
"""

from __future__ import annotations

import math
import threading
from typing import Any

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gdk, Gtk, Pango  # noqa: E402

from .app import OmenApp  # noqa: E402

WAVE = "▁▂▃▄▅▆▇█▇▆▅▄▃▂"


class VoiceOrb(Gtk.Window):
    """An always-on-top, click-to-talk orb driven by the local Omen voice stack."""

    def __init__(self) -> None:
        Gtk.Window.__init__(self, title="Omen Voice")
        self.set_default_size(190, 250)
        self.set_resizable(False)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_app_paintable(True)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)

        self.app = OmenApp(output=self._on_output)
        self._recording = False
        self._frame = 0
        self._busy = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        self.add(box)

        self.drawing = Gtk.DrawingArea()
        self.drawing.set_size_request(160, 160)
        self.drawing.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
        )
        self.drawing.connect("draw", self._draw)
        self.drawing.connect("button-press-event", self._on_press)
        self.drawing.connect("button-release-event", self._on_release)
        box.pack_start(self.drawing, False, False, 0)

        self.status = Gtk.Label(label="Hold to talk")
        self.status.set_line_wrap(True)
        self.status.set_lines(3)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(24)
        box.pack_start(self.status, False, False, 0)

        self.hint = Gtk.Label(label="release to send")
        self.hint.set_markup('<span foreground="#888888" size="small">release to send</span>')
        box.pack_start(self.hint, False, False, 0)

        self._motion_origin: tuple[int, int] | None = None
        self.connect("button-press-event", self._on_window_press)
        self.connect("motion-notify-event", self._move)
        self.add_events(Gdk.EventMask.BUTTON_MOTION_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self._tick_id = GLib.timeout_add(90, self._tick)
        self.show_all()

    def _on_window_press(self, _widget: Any, event: Any) -> None:
        if event.button == 1:
            self._motion_origin = (int(event.x), int(event.y))
        elif event.button == 3:
            self.destroy()

    def _move(self, _widget: Any, event: Any) -> None:
        if self._motion_origin is None or not event.state & Gdk.ModifierType.BUTTON1_MASK:
            return
        x, y = self.get_position()
        self.move(x + int(event.x) - self._motion_origin[0], y + int(event.y) - self._motion_origin[1])

    def _on_output(self, text: str) -> None:
        GLib.idle_add(self._set_status, text)

    def _set_status(self, text: str) -> None:
        clean = " ".join(str(text).split())
        if clean:
            self.status.set_label(clean[:140])

    def _on_press(self, _widget: Any, event: Any) -> bool:
        if event.button != 1 or self._recording or self._busy:
            return True
        self._recording = True
        self._set_status("Listening…")
        threading.Thread(target=self._begin, daemon=True).start()
        self.drawing.queue_draw()
        return True

    def _on_release(self, _widget: Any, event: Any) -> bool:
        if event.button != 1 or not self._recording:
            return True
        self._recording = False
        self._busy = True
        self._set_status("Thinking…")
        threading.Thread(target=self._finish, daemon=True).start()
        self.drawing.queue_draw()
        return True

    def _begin(self) -> None:
        try:
            self.app.voice.begin_push_to_talk()
        except Exception as exc:
            GLib.idle_add(self._set_status, f"Error: {type(exc).__name__}: {exc}")
            self._recording = False

    def _finish(self) -> None:
        try:
            result = self.app.voice.end_push_to_talk()
            if not result.get("accepted"):
                GLib.idle_add(self._set_status, "No speech detected. Try again.")
        except Exception as exc:
            GLib.idle_add(self._set_status, f"Error: {type(exc).__name__}: {exc}")
        finally:
            self._busy = False
            self.drawing.queue_draw()

    def _tick(self) -> bool:
        if self._recording:
            self._frame += 1
            self.drawing.queue_draw()
        return True

    def _draw(self, _widget: Any, cr: Any) -> None:
        width = self.drawing.get_allocated_width()
        height = self.drawing.get_allocated_height()
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 10
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()

        phase = self._frame if self._recording else 0
        for layer in range(5, 0, -1):
            spread = 1.0 + layer * (0.06 + (math.sin(phase / 4 + layer) + 1) * 0.03 if self._recording else 0.02)
            alpha = 0.16 - layer * 0.02
            cr.set_source_rgba(0.55, 0.25, 1.0, max(alpha, 0.03))
            cr.arc(cx, cy, radius * spread, 0, 2 * math.pi)
            cr.fill()

        gradient = cairo.RadialGradient(cx - radius * 0.35, cy - radius * 0.4, radius * 0.05, cx, cy, radius)
        if self._recording:
            gradient.add_color_stop_rgba(0, 0.95, 0.75, 1.0, 1)
            gradient.add_color_stop_rgba(0.55, 0.55, 0.25, 1.0, 1)
            gradient.add_color_stop_rgba(1, 0.18, 0.05, 0.45, 1)
        else:
            gradient.add_color_stop_rgba(0, 0.75, 0.65, 1.0, 1)
            gradient.add_color_stop_rgba(0.6, 0.35, 0.2, 0.8, 1)
            gradient.add_color_stop_rgba(1, 0.1, 0.05, 0.3, 1)
        cr.set_source(gradient)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.set_line_width(1.5)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()

        bars = 9
        for index in range(bars):
            offset = (index - (bars - 1) / 2) * 9
            if self._recording:
                magnitude = 0.35 + 0.65 * abs(math.sin(phase / 3 + index * 0.9))
            else:
                magnitude = 0.25
            bar_height = 10 + 34 * magnitude
            x = cx + offset
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.set_line_width(4)
            cr.move_to(x, cy - bar_height / 2)
            cr.line_to(x, cy + bar_height / 2)
            cr.stroke()

    def close(self) -> None:
        self.app.close()


def main() -> int:
    orb = VoiceOrb()
    orb.connect("destroy", Gtk.main_quit)
    orb.connect("destroy", lambda _w: orb.close())
    try:
        Gtk.main()
    except KeyboardInterrupt:
        orb.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
