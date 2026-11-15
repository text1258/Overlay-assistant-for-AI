#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
nohup "$PY" src/overlay.py >/dev/null 2>&1 &
sleep 1
osascript -e 'tell application "Terminal" to close (every window whose name contains "start_overlay_macos")' >/dev/null 2>&1 &
exit 0
