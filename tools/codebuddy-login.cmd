@echo off
chcp 65001 >nul
title CodeBuddy CLI - one-time login
cd /d "%~dp0.."

set "CBC=C:\Users\CfLks\.codebuddy-cli\codebuddy.cmd"

if not exist "%CBC%" (
  echo [ERROR] Official CodeBuddy CLI not found at:
  echo         %CBC%
  echo         Install it first:
  echo         npm install -g --prefix "C:/Users/CfLks/.codebuddy-cli" @tencent-ai/codebuddy-code
  pause
  exit /b 1
)

echo ============================================================
echo  CodeBuddy CLI login helper
echo ------------------------------------------------------------
echo  1) Type  /login  and press Enter
echo  2) Finish authorization in the browser, it returns here
echo  3) Back here, type  /exit  (or Ctrl+C twice)
echo ============================================================
echo.

call "%CBC%" %*

echo.
echo [exited]
pause
