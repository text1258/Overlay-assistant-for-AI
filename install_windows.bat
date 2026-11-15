@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка зависимостей ===
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r src\requirements.txt
if errorlevel 1 (
  echo.
  echo Не получилось. Проверь, что установлен Python 3.10+ с python.org
  echo и при установке была галочка "Add Python to PATH".
  pause
  exit /b 1
)
echo.
echo Готово. Запускай start_manager_windows.bat — чаты и настройки,
echo а start_overlay_windows.bat — оверлей. Claude Code и вход в аккаунт
echo можно сделать прямо в программе: Настройки - Аккаунт.
pause
