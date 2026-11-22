@echo off
rem Re-run the automated verification on THIS PC and regenerate tests\results\RESULTS.md (about 8-10 minutes).
rem The pipeline tests use a stand-in detector on purpose (known answers); your real model is tested by tools\acceptance.bat.
cd /d "%~dp0\.."
set PYTHONUTF8=1
python -m pip install -q pytest pymavlink pypdf
if exist tests\results\evidence.json del tests\results\evidence.json
python -m pytest tests -v --junitxml=tests\results\junit.xml
python tests\make_report.py
echo.
echo Results: tests\results\RESULTS.md   (send it, or the failing test's output, if anything fails)
pause
