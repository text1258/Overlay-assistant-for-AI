@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка зависимостей ===
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Не получилось. Проверь, что установлен Python 3.10+ с python.org
  echo и при установке была галочка "Add Python to PATH".
  pause
  exit /b 1
)
echo.
echo === Проверка Claude Code ===
where claude >nul 2>nul
if errorlevel 1 (
  echo Claude Code не найден. Установи его командой в PowerShell:
  echo     irm https://claude.ai/install.ps1 ^| iex
  echo Затем открой новое окно терминала, запусти  claude  и войди аккаунтом с подпиской.
) else (
  claude --version
  echo Claude Code найден. Если ещё не входил - запусти в терминале  claude  и войди.
)
echo.
echo Готово. Запускай start.bat
pause
