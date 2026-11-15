#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "Сеанс Wayland: горячие клавиши и скриншоты могут не работать — нужен сеанс Xorg."
fi
nohup "$PY" src/overlay.py >/dev/null 2>&1 &
