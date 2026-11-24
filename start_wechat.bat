@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo ============================================================
echo   DaFeiYu Bot + Watchdog (WeChat + Web: http://127.0.0.1:8765)
echo ============================================================
where python >nul 2>&1
if errorlevel 1 goto use_py
python watchdog.py --mode both
pause
exit /b 0

:use_py
py -3 watchdog.py --mode both
pause
