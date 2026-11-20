@echo off
rem Run from this checkout, with the configured port and a readiness-based browser launch.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" altera.py --open
) else (
    echo Create the virtual environment and install requirements first. See README.md.
    exit /b 1
)
pause
