@echo off
title Distill
cd /d "%~dp0"

rem ---- find Python 3.10+ ----
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython

rem ---- first run: virtual environment + dependencies ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)
call ".venv\Scripts\activate.bat"
if not exist ".venv\deps-installed.txt" (
  echo Installing dependencies - first run only, takes a few minutes...
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  if errorlevel 1 goto :fail
  echo Downloading the headless browser for JavaScript sites...
  python -m playwright install chromium
  echo ok> ".venv\deps-installed.txt"
)
if not exist ".env" copy ".env.example" ".env" >nul

rem ---- run: app on http://localhost:8000, demo site on http://localhost:8765 ----
echo.
echo Starting Distill. Close this window or press Ctrl+C to stop.
python run.py --demo
pause
goto :eof

:nopython
echo.
echo Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then run start.bat again.
pause
goto :eof

:fail
echo.
echo Setup failed. Read the messages above, then run start.bat again.
pause
