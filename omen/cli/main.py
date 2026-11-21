"""Interactive terminal chat entry point."""

from __future__ import annotations

import sys
import queue
import select
import threading

from ..app import OmenApp
from .commands import handle_command
from .render import render_tool_progress


def main() -> int:
    app = OmenApp(output=lambda text: print(text, flush=True))
    confirmations: queue.Queue[dict] = queue.Queue()

    def confirm_voice(transcript: str, confidence: float) -> bool:
        request = {"transcript": transcript, "confidence": confidence, "resolved": threading.Event(), "confirmed": False}
        confirmations.put(request)
        if not request["resolved"].wait(timeout=120):
            return False
        return bool(request["confirmed"])

    app.voice.confirmation = confirm_voice
    voice_status = app.voice.start_hotkey_listener()
    print("Omen — local-first assistant. /help for commands, /bye to exit.", flush=True)
    if voice_status.get("started"):
        state = app.voice.status()
        if state.get("blocked_external"):
            print("[voice] BLOCKED-EXTERNAL: see /status for local speech requirements.", flush=True)
        else:
            print(f"[voice] Push-to-talk ready on {state['hotkey']}.", flush=True)
    pending_confirmation = None
    prompt_state = None
    try:
        while True:
            if pending_confirmation is None and not confirmations.empty():
                pending_confirmation = confirmations.get_nowait()
                print(
                    f"\n[voice confirmation] Transcript: {pending_confirmation['transcript']} "
                    f"(confidence {pending_confirmation['confidence']:.0%}). Confirm? [y/N]",
                    flush=True,
                )
                prompt_state = None
            desired_prompt = "confirm" if pending_confirmation is not None else "omen"
            if prompt_state != desired_prompt:
                print("confirm> " if desired_prompt == "confirm" else "omen> ", end="", flush=True)
                prompt_state = desired_prompt
            ready, _, _ = select.select([sys.stdin], [], [], 0.25)
            if not ready:
                continue
            line = sys.stdin.readline()
            if not line:
                print(flush=True)
                break
            line = line.rstrip("\n")
            prompt_state = None
            if pending_confirmation is not None:
                pending_confirmation["confirmed"] = line.strip().lower() in {"y", "yes"}
                pending_confirmation["resolved"].set()
                print("[voice] Transcript confirmed." if pending_confirmation["confirmed"] else "[voice] Transcript declined.", flush=True)
                pending_confirmation = None
                continue
            if not line.strip():
                continue
            if handle_command(app, line, lambda text: print(text, flush=True)):
                if line.strip().lower() == "/bye":
                    break
                continue
            try:
                for token in app.stream_chat(
                    line,
                    on_token=lambda delta: print(delta, end="", flush=True),
                    on_progress=lambda result: print(render_tool_progress(result), flush=True),
                ):
                    pass
                print(flush=True)
            except Exception as exc:
                # Fail loudly: surface the real error instead of swallowing it.
                print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted. External actions locked. Re-run /kill if needed.", flush=True)
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
