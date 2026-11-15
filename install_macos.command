#!/usr/bin/env bash
# Установка для macOS: двойной щелчок в Finder
cd "$(dirname "$0")" || exit 1
PY=python3
command -v $PY >/dev/null || { echo "Нужен Python 3.10+ с python.org"; read -r -p "Enter — закрыть"; exit 1; }
if ! $PY -c "import tkinter" 2>/dev/null; then
  echo "Нет модуля tkinter. Поставь Python с python.org или: brew install python-tk"
  read -r -p "Enter — закрыть"; exit 1
fi
[ -d .venv ] || $PY -m venv .venv || { read -r -p "Не удалось создать .venv. Enter — закрыть"; exit 1; }
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r src/requirements.txt || { read -r -p "Ошибка. Enter — закрыть"; exit 1; }
chmod +x start_manager_macos.command start_overlay_macos.command 2>/dev/null
echo
echo "Готово. Запускай start_manager_macos.command (чаты и настройки)"
echo "и start_overlay_macos.command (оверлей)."
echo
echo "При первом запуске macOS спросит разрешения — их нужно дать Терминалу (или Python):"
echo "  • Конфиденциальность → Универсальный доступ — для горячих клавиш;"
echo "  • Конфиденциальность → Запись экрана — для скриншотов."
read -r -p "Enter — закрыть"
