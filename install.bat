@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo ============================================================
echo   DaFeiYu Bot - Dependency Installer
echo ============================================================
where python >nul 2>&1
if errorlevel 1 goto try_py
set PY_CMD=python
goto do_install

:try_py
where py >nul 2>&1
if errorlevel 1 goto no_python
set PY_CMD=py -3
goto do_install

:no_python
echo [ERROR] Python not found in PATH!
echo Please install Python 3.10~3.13 (64-bit) from https://www.python.org
echo Remember to check "Add Python to PATH" during installation.
pause
exit /b 1

:do_install
%PY_CMD% --version
echo Installing packages via Tsinghua PyPI mirror...
%PY_CMD% -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
%PY_CMD% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo ============================================================
echo   Done! Now double-click start_wechat.bat
echo ============================================================
pause
