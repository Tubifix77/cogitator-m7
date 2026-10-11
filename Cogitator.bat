@echo off
rem One-click launcher: starts the host bridge (server.py), wakes Ollama if needed,
rem and opens the console in your default browser. Close this window to shut down.
title Cogitator M-VII - close this window to shut down
cd /d "%~dp0"

where python >nul 2>nul || (
    echo Python 3 is required: https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import psutil" 2>nul || (
    echo First run: installing the psutil package for hardware telemetry...
    python -m pip install --user psutil || ( pause & exit /b 1 )
)

python server.py %*
if errorlevel 1 pause
