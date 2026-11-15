@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw manager.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw manager.py & exit /b)
python manager.py
