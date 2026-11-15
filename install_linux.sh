#!/usr/bin/env bash
# Установка для Linux: виртуальное окружение .venv и зависимости
cd "$(dirname "$0")" || exit 1
PY=python3
command -v $PY >/dev/null || { echo "Нужен Python 3.10+: sudo apt install python3"; exit 1; }
if ! $PY -c "import tkinter" 2>/dev/null; then
  echo "Нет модуля tkinter. Установи его:"
  echo "  Ubuntu/Debian: sudo apt install python3-tk"
  echo "  Fedora:        sudo dnf install python3-tkinter"
  echo "  Arch:          sudo pacman -S tk"
  exit 1
fi
if [ ! -d .venv ]; then
  $PY -m venv .venv || { echo "Не удалось создать .venv. Ubuntu/Debian: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r src/requirements.txt || exit 1
chmod +x start_manager_linux.sh start_overlay_linux.sh 2>/dev/null
echo
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "Внимание: сейчас сеанс Wayland. Глобальные горячие клавиши и скриншоты"
  echo "работают в сеансе Xorg — выбери его на экране входа (\"Ubuntu on Xorg\")."
fi
echo "Готово. Запускай ./start_manager_linux.sh (чаты и настройки) и ./start_overlay_linux.sh (оверлей)."
