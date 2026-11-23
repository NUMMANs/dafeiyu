@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [INFO] Created .env from .env.example.
)
echo Starting DaFeiYu Web Dashboard: http://127.0.0.1:8765
python main.py --mode web
pause
