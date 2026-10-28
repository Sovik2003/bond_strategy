@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem Launches the bond_strategy Streamlit UI on Windows.
rem Place this file in the repository root (next to app.py, pyproject.toml).

cd /d "%~dp0"

echo ============================================
echo   bond_strategy - Streamlit UI
echo ============================================
echo.

rem --- Python ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo During install, check "Add Python to PATH".
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%

rem --- Virtual environment ---
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate venv.
    pause
    exit /b 1
)

rem --- Dependencies ---
echo.
echo Checking dependencies...
python -c "import streamlit" >nul 2>nul
if errorlevel 1 (
    echo Installing package and streamlit, this takes a couple of minutes...
    pip install --quiet --upgrade pip
    pip install --quiet -e ".[dev]"
    pip install --quiet streamlit
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
) else (
    echo Dependencies already installed.
)

rem --- Core test check (does not block the launch on failure) ---
echo.
echo Running core tests...
python -m pytest -q
if errorlevel 1 (
    echo.
    echo [WARNING] Tests did not pass. The app will still start,
    echo but the pipeline results may be unreliable.
    echo.
    pause
)

rem --- Launch ---
echo.
echo Starting Streamlit. Your browser will open automatically.
echo Press Ctrl+C in this window to stop the server.
echo.

set PYTHONPATH=src
streamlit run app.py

endlocal
