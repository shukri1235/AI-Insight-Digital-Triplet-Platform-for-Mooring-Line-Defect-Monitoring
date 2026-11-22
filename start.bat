@echo off
rem Mooring Twin - one-click start (Windows).
rem Builds the web UI when it is missing or out of date (needs Node.js once), then starts the backend,
rem which serves the UI and opens it in your browser. Close this window (or press Ctrl+C) to stop.
rem   start.bat --rebuild   force a fresh UI build
setlocal
cd /d "%~dp0"
title Mooring Twin
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
where python >nul 2>nul || (echo Python was not found. Install Python 3.10+ and run: pip install -r backend\requirements.txt & pause & exit /b 1)
python -c "import fastapi, uvicorn, sqlalchemy, cv2, reportlab" 2>nul || (echo Some Python packages are missing. Run:  pip install -r backend\requirements.txt & pause & exit /b 1)

set BUILD=0
if /i "%~1"=="--rebuild" set BUILD=1
python tools\ui_check.py >nul || set BUILD=1
if "%BUILD%"=="1" goto build
goto run

:build
where npm >nul 2>nul
if errorlevel 1 (
  if exist "frontend\dist\index.html" (
    echo [UI] Node.js not found - using the existing UI build, which may be out of date.
    goto run
  )
  echo [UI] The web UI is not built and Node.js is not installed.
  echo      Install Node.js LTS from https://nodejs.org, then run start.bat again.
  pause
  exit /b 1
)
echo [UI] Building the web UI (first run or after an update, about a minute)...
pushd frontend
if not exist node_modules call npm install
if errorlevel 1 (popd & echo [UI] npm install failed. & pause & exit /b 1)
call npm run build
if errorlevel 1 (popd & echo [UI] The UI build failed - see the messages above. & pause & exit /b 1)
popd

:run
echo [APP] Starting Mooring Twin - the browser opens when it is ready. Close this window to stop.
python backend\main.py --open
if errorlevel 1 pause
