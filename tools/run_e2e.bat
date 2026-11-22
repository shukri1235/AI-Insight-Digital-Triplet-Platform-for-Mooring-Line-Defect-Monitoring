@echo off
rem Browser walkthrough (76 checks) on THIS PC: Chromium clicks through every screen against a test backend.
rem Uses temporary test databases and footage; your real data is never touched. About 5 minutes.
rem First run downloads Playwright and Chromium (about 150 MB). Close the Mooring Twin app before running.
cd /d "%~dp0\.."
set PYTHONUTF8=1
where node >nul 2>nul
if errorlevel 1 (echo Node.js is needed: install Node.js LTS from https://nodejs.org, then run this again. & pause & exit /b 1)
if not exist frontend\dist\index.html (echo The web UI is not built yet: run start.bat once, close the app, then run this again. & pause & exit /b 1)
python -m pip install -q pymavlink
pushd tests\e2e
if not exist node_modules\playwright (
  echo Installing Playwright for the walkthrough...
  call npm install --no-audit --no-fund
  if errorlevel 1 (popd & echo npm install failed. & pause & exit /b 1)
)
call npx playwright install chromium
if errorlevel 1 (popd & echo Chromium download failed. & pause & exit /b 1)
popd
python tools\run_e2e.py
python tests\make_report.py
echo.
echo Results: tests\results\RESULTS.md (the header says which PC the walkthrough ran on); screenshots in tests\results\e2e
pause
