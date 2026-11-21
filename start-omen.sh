#!/usr/bin/env bash
# Launch Omen with the local voice environment and keyboard access.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
export PATH="$ROOT/.venv/bin:$PATH"
export VIRTUAL_ENV="$ROOT/.venv"
exec newgrp input -c "exec '$ROOT/.venv/bin/python' -m omen.cli.main"
