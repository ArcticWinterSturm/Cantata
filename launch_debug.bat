@echo off
REM Start Cantata with a visible console (tracebacks, Qt warnings).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
) else (
    python run.py %*
)
echo.
pause
