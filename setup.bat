@echo off
setlocal
rem Explicit setup only. No server, speech service or model is started.
rem Python 3.11, 3.12 or 3.13. The launcher's 3.12 is tried first: the local voice runs on it too.
if defined ALTERA_SETUP_PYTHON (
    "%ALTERA_SETUP_PYTHON%" -I -B "%~dp0scripts\setup_altera.py" %*
    exit /b
)
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -I -B "%~dp0scripts\setup_altera.py" %*
    exit /b
)
set "FOUND="
for %%V in (3.12 3.13 3.11) do if not defined FOUND (
    py -%%V -c "import sys" >nul 2>&1 && set "FOUND=%%V"
)
if defined FOUND goto launcher
python -c "import sys" >nul 2>&1
if not errorlevel 1 goto plain
echo Python was not found. Install Python 3.12 from python.org (3.11 and 3.13 also work), then run setup.bat again.
exit /b 1
:launcher
py -%FOUND% -I -B "%~dp0scripts\setup_altera.py" %*
exit /b %errorlevel%
:plain
rem Setup checks this Python's version itself and says so if it is not 3.11, 3.12 or 3.13.
python -I -B "%~dp0scripts\setup_altera.py" %*
exit /b %errorlevel%
