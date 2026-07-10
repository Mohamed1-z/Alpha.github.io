@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Run install_packages.bat after installing Python, then try again.
    pause
    exit /b 1
)

python -u app.py --export-alert-report
pause
