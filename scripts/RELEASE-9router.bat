@echo off
REM ============================================================
REM  FAILSAFE: double-click this if the chat stays frozen.
REM  It resumes (unfreezes) the 9router :443 process so the
REM  normal 1.23 chat works again.
REM ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ninerouter_isolation.ps1" -Action release
echo.
echo Done. 9router resumed. You can close this window.
pause
