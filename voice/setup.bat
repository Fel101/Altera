@echo off
rem Install Altera's local voice service: its own environment in voice\.venv, then Kokoro's model and voices.
rem Kokoro needs Python 3.10, 3.11 or 3.12; this uses the Python launcher's 3.12 unless PYTHON is set.
setlocal
set "HERE=%~dp0"
if "%PYTHON%"=="" set "PYTHON=py -3.12"
if not exist "%HERE%.venv\Scripts\python.exe" (
    echo Making %HERE%.venv
    %PYTHON% -m venv "%HERE%.venv" || goto failed
)
"%HERE%.venv\Scripts\python.exe" -m pip install --upgrade pip || goto failed
"%HERE%.venv\Scripts\python.exe" -m pip install -r "%HERE%requirements.txt" || goto failed
"%HERE%.venv\Scripts\python.exe" "%HERE%serve.py" --download || goto failed
echo.
echo The voice service is installed. In Altera's Settings, switch Voice on.
exit /b 0
:failed
echo.
echo The voice service was not installed. The message above says why.
exit /b 1
