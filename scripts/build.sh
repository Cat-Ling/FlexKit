#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ -z "${VENV_DIR:-}" ]; then
    probe="$ROOT_DIR/.symlink_probe_$$"
    if ln -s . "$probe" 2>/dev/null; then
        rm -f "$probe"
        VENV_DIR="$ROOT_DIR/.venv-build"
    else
        rm -f "$probe"
        VENV_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/flexkit-build-venv"
    fi
fi

if [ ! -d "$VENV_DIR/bin" ]; then
    python3 -m venv "$VENV_DIR"
fi
VENV_DIR="$(CDPATH= cd -- "$VENV_DIR" && pwd -P)"

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r requirements-dev.txt
"$VENV_DIR/bin/python" -m pytest -q
"$VENV_DIR/bin/python" scripts/build.py "$@"
