@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw overlay.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw overlay.py & exit /b)
python overlay.py
