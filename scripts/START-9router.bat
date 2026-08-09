@echo off
REM ============================================================
REM  START 9router manually  (temporary :443 control).
REM
REM  9router is NO LONGER auto-started at boot. Double-click this
REM  whenever you want it back (e.g. for the 1.23 Antigravity chat
REM  or other MITM tools). It will bind :443 per its saved state.
REM
REM  To make it permanent again: rename
REM     Startup\9router.vbs.disabled  ->  Startup\9router.vbs
REM ============================================================
start "" "D:\Program Files\nodejs\node.exe" "D:\npm\npm-global\node_modules\9router\cli.js" --tray --skip-update
echo 9router launched (tray icon). Give it ~5s to bind :443.
timeout /t 4 >nul
