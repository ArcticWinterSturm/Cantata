@echo off
REM Start Cantata without a console window.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" run.py %*
) else (
    echo Run install.bat first.
    pause
)
