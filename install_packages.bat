@echo off
setlocal

cd /d "%~dp0"

echo Installing TradeView Python packages...
echo Project folder: %CD%
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python first, then run this file again.
    pause
    exit /b 1
)

echo Using Python:
python --version
echo.

echo Making sure pip is available...
python -m ensurepip --upgrade
if errorlevel 1 (
    echo Failed to prepare pip.
    pause
    exit /b 1
)

echo.
echo Upgrading pip tools...
python -m pip install --user --upgrade pip setuptools wheel
if errorlevel 1 (
    echo Failed to upgrade pip tools.
    pause
    exit /b 1
)

echo.
echo Installing packages from requirements.txt...
python -m pip install --user -r requirements.txt
if errorlevel 1 (
    echo Package installation failed.
    echo Check the error above, then run this file again.
    pause
    exit /b 1
)

echo.
echo Done. You can now run app.py again.
pause
