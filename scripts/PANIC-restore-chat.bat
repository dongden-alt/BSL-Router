@echo off
REM ============================================================
REM  PANIC RESTORE  —  double-click if this chat goes dark after
REM  the BSL :443 cutover and does NOT come back.
REM
REM  It (1) tells BSL to release :443, (2) force-frees :443 if a
REM  stray mitmdump remains, then (3) relaunches 9router so the
REM  normal 1.23 chat path (cloudcode-pa -> :443 -> 9router) works
REM  again.
REM ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:6969/api/mitm/stop' -TimeoutSec 8 | Out-Null; Write-Host '[panic] asked BSL to release :443' } catch { Write-Host '[panic] BSL stop call failed (may be down) — continuing' };" ^
  "Start-Sleep 2;" ^
  "$o=(Get-NetTCPConnection -LocalPort 443 -State Listen -EA 0 | Select-Object -First 1).OwningProcess;" ^
  "if($o){ $n=(Get-CimInstance Win32_Process -Filter (\"ProcessId=\"+$o)).Name; if($n -match 'mitmdump'){ taskkill /T /F /PID $o 2>$null | Out-Null; Write-Host ('[panic] killed stray mitmdump PID '+$o) } else { Write-Host ('[panic] :443 held by '+$n+' PID '+$o+' — leaving it') } } else { Write-Host '[panic] :443 is free' }"
echo.
echo [panic] Relaunching 9router...
start "" "D:\Program Files\nodejs\node.exe" "D:\npm\npm-global\node_modules\9router\cli.js" --tray --skip-update
echo [panic] Done. Give 9router ~5s to bind :443, then this chat should work again.
timeout /t 5 >nul
