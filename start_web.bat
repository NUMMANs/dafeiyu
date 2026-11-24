@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if exist ".env" goto run_web
copy ".env.example" ".env" >nul
echo [INFO] Created .env from .env.example.
:run_web
echo Starting DaFeiYu Web Dashboard + Watchdog: http://127.0.0.1:8765
where python >nul 2>&1
if errorlevel 1 goto use_py
python watchdog.py --mode web
pause
exit /b 0

:use_py
py -3 watchdog.py --mode web
pause
