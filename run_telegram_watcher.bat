@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Project virtual environment was not found.
    echo Run: python -m venv .venv
    echo Then run: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -u app.py --interval 30 --telegram-only
if errorlevel 1 (
    echo.
    echo Watcher stopped with an error.
    pause
    exit /b 1
)
