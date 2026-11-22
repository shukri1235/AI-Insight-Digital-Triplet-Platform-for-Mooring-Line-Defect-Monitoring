@echo off
rem Model acceptance tests (MA-1..4) on THIS PC with YOUR weights.
rem   tools\acceptance.bat <best.pt> [data.yaml] [folder of defect-free real frames]
rem Optional targets (set before running): FYP_MIN_MAP50 (0.80), FYP_MIN_DEFORM_RECALL (0.90),
rem   FYP_VAL_SPLIT (val), FYP_TRIGGER (0.60), FYP_MAX_FALSE_ALARM (0.01)
setlocal
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: tools\acceptance.bat path\to\best.pt [data.yaml] [clean frames folder] & exit /b 1)
set "FYP_REAL_WEIGHTS=%~f1"
if not "%~2"=="" set "FYP_VAL_DATA=%~f2"
if not "%~3"=="" set "FYP_CLEAN_FRAMES=%~f3"
python -m pytest tests\test_model_acceptance.py -v -s -p no:cacheprovider
pause
