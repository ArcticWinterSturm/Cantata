@echo off
REM Cantata installer (Windows): creates .venv and installs PyQt6 for the GUI.
REM The CLI needs nothing: python run.py cli --help
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
echo Creating virtual environment...
%PY% -m venv .venv || goto :fail
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :fail
echo.
echo Cantata is installed. Start it with launch.bat  (or launch_debug.bat to see the console).
pause
exit /b 0
:fail
echo.
echo Installation failed - is Python 3.10+ installed and on PATH?
pause
exit /b 1
