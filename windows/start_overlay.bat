@echo off
cd /d "%~dp0.."
where pyw >nul 2>nul && (start "" pyw src\overlay.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw src\overlay.py & exit /b)
python src\overlay.py
