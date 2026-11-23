@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo ============================================================
echo   DaFeiYu Bot (QQ OneBot11 ws://127.0.0.1:8765/onebot/ws)
echo ============================================================
where python >nul 2>&1
if errorlevel 1 goto use_py
python main.py --mode qq
pause
exit /b 0

:use_py
py -3 main.py --mode qq
pause
