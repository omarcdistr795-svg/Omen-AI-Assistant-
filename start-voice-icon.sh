#!/usr/bin/env bash
# Launch the floating Omen voice orb (click-and-hold push-to-talk).
# System Python supplies GTK; the venv site-packages supply vosk/piper/evdev.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
SITE="$("$ROOT/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
export PYTHONPATH="$SITE${PYTHONPATH:+:$PYTHONPATH}"
export VIRTUAL_ENV="$ROOT/.venv"
exec /usr/bin/python3 -m omen.voice_icon
