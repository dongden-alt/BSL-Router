<#
    BSL Router smoke battery — the pre-restart gate.

    Usage:
        # Staging instance first (safe — never touches live IDE traffic):
        .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 6970
        .\scripts\smoke_test.ps1 -Port 6970          # must be ALL GREEN

        # Only then restart the live router and re-run against it:
        .\scripts\smoke_test.ps1                     # defaults to 6969

    Exit code 0 = all checks green; 1 = any failure. Use as a restart gate:
        if (-not (.\scripts\smoke_test.ps1 -Port 6970)) { throw "restart aborted" }

    Notes:
      * Probes are tiny (max_tokens 16) — quota impact negligible.
      * A GREEN run records only successes (clears streaks, no bans).
      * A RED run on a staging instance shares config.yaml with prod: failing
        leaves get softbanned. That is signal, not damage — but expect the
        same failures if you immediately re-probe prod.
      * /v1internal probe uses explicit alias headers, so it exercises the
        full mapped-Gemini rail WITHOUT needing the MITM hijack (which is
        pinned to the live port).
#>
[CmdletBinding()]
param(
    [int]$Port = 6969,
    [string]$ApiKey = "sk-bsl-YOUR_API_KEY_HERE",
    [string]$Model = "GLM-5.3",
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Continue'
$Base = "http://127.0.0.1:$Port"
$Results = [System.Collections.Generic.List[object]]::new()

function Add-Result([string]$Name, [bool]$Ok, [string]$Detail) {
    $script:Results.Add([pscustomobject]@{ Check = $Name; Ok = $Ok; Detail = $Detail })
    $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
    Write-Output ("[{0}] {1} {2}" -f $mark, $Name, $Detail)
}

# 1. Model catalog ------------------------------------------------------------
try {
    $r = Invoke-WebRequest -Uri "$Base/v1/models" -Headers @{Authorization = "Bearer $ApiKey"} -TimeoutSec 30 -UseBasicParsing
    $n = (($r.Content | ConvertFrom-Json).data | Measure-Object).Count
    Add-Result 'catalog' ($r.StatusCode -eq 200 -and $n -gt 0) ("$($r.StatusCode), $n models")
} catch { Add-Result 'catalog' $false $_.Exception.Message }

# 2. Chat non-stream ----------------------------------------------------------
try {
    $b = @{model = $Model; messages = @(@{role='user'; content='reply with exactly: ok'}); max_tokens = 16; stream = $false} | ConvertTo-Json -Depth 5
    $r = Invoke-WebRequest -Uri "$Base/v1/chat/completions" -Method POST -Headers @{Authorization = "Bearer $ApiKey"} -ContentType 'application/json' -Body $b -TimeoutSec $TimeoutSec -UseBasicParsing
    $j = $r.Content | ConvertFrom-Json
    $txt = $j.choices[0].message.content
    Add-Result 'chat-nonstream' ($r.StatusCode -eq 200 -and $txt) ("$($r.StatusCode) via $($j.model): $($txt.Substring(0,[Math]::Min(40,$txt.Length)))")
} catch { Add-Result 'chat-nonstream' $false $_.Exception.Message }

# 3. Chat stream (first SSE chunk) -------------------------------------------
try {
    $b = @{model = $Model; messages = @(@{role='user'; content='say ok'}); max_tokens = 16; stream = $true} | ConvertTo-Json -Depth 5
    $r = Invoke-WebRequest -Uri "$Base/v1/chat/completions" -Method POST -Headers @{Authorization = "Bearer $ApiKey"} -ContentType 'application/json' -Body $b -TimeoutSec $TimeoutSec -UseBasicParsing
    $ok = ($r.StatusCode -eq 200) -and ($r.Content -match '^data: ')
    Add-Result 'chat-stream' $ok ("$($r.StatusCode), $($r.Content.Length) bytes SSE")
} catch { Add-Result 'chat-stream' $false $_.Exception.Message }

# 4. Mapped-Gemini rail (Antigravity /v1internal with explicit alias) ---------
try {
    $body = @{model = $Model; contents = @(@{role = 'user'; parts = @(@{text = 'say ok'})})} | ConvertTo-Json -Depth 8
    $r = Invoke-WebRequest -Uri "$Base/v1internal`:streamGenerateContent?alt=sse" -Method POST -Headers @{Authorization = "Bearer $ApiKey"; 'x-bsl-antigravity-alias' = $Model; 'x-bsl-antigravity-source-model' = 'smoke-test'} -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec -UseBasicParsing
    $ok = ($r.StatusCode -eq 200) -and ($r.Content -match 'data: \{') -and ($r.Content -match '\[DONE\]') -and ($r.Content -notmatch 'upstream error')
    Add-Result 'mapped-gemini-sse' $ok ("$($r.StatusCode), $($r.Content.Length) bytes, DONE=$($r.Content -match '\[DONE\]')")
} catch { Add-Result 'mapped-gemini-sse' $false $_.Exception.Message }

# 5. Admin UI ------------------------------------------------------------------
try {
    $r = Invoke-WebRequest -Uri "$Base/admin/" -TimeoutSec 30 -UseBasicParsing
    Add-Result 'admin-ui' ($r.StatusCode -eq 200) $r.StatusCode
} catch { Add-Result 'admin-ui' $false $_.Exception.Message }

# Summary -----------------------------------------------------------------------
$failed = @($Results | Where-Object { -not $_.Ok }).Count
Write-Output ''
Write-Output ("SMOKE {0}: {1}/{2} checks passed (port {3})" -f $(if ($failed -eq 0) {'GREEN'} else {'RED'}), ($Results.Count - $failed), $Results.Count, $Port)
if ($failed -gt 0) { exit 1 } else { exit 0 }
