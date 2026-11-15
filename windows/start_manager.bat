@echo off
cd /d "%~dp0.."
where pyw >nul 2>nul && (start "" pyw src\manager.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw src\manager.py & exit /b)
python src\manager.py
