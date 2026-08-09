#requires -Version 5.1
<#
  ninerouter_isolation.ps1  —  BSL Router <-> 9router isolation toggle

  PURPOSE
    Temporarily SUSPEND (freeze) the 9router process that owns :443 so it cannot
    serve OR re-steal the port, giving Antigravity 2.1.1 a clean, uncontested path
    to BSL Router on 127.0.0.1:6969. RESUME restores 9router so the normal
    1.23 chat works again.

  WHY SUSPEND (not kill)
    - Frozen process keeps ownership of the :443 socket => no port-steal race.
    - Fully reversible in milliseconds (NtResumeProcess).
    - No need to relaunch 9router or fight the startup-folder watchdog.

  ACTIONS
    -Action isolate   Freeze 9router now (manual button ON).
    -Action release   Unfreeze 9router now (manual button OFF / failsafe).
    -Action status    Show current 9router + port + 2.1.1 state.
    -Action test      Full bounded probe: ensure 2.1.1 up -> isolate -> watch
                      BSL for inference for -WindowSeconds -> ALWAYS release.

  SAFETY
    - 'test' wraps isolation in try/finally so 9router is ALWAYS resumed,
      even on error/Ctrl-C, so this chat can never be left frozen.
#>

param(
  [ValidateSet('isolate','release','status','test')]
  [string]$Action = 'status',
  [int]$WindowSeconds = 150
)

$ErrorActionPreference = 'Stop'

# Paths are auto-detected from this script's location so the utility works for
# any clone. Override via environment variables when your layout differs:
#   BSL_ROUTER_ROOT - repository root
#   BSL_IDE_PATH    - full path to the IDE executable used by the 'test' action
$RepoRoot = if ($env:BSL_ROUTER_ROOT) { $env:BSL_ROUTER_ROOT } else { Split-Path -Parent $PSScriptRoot }
$BslLog   = Join-Path $RepoRoot '.brain\logs\app.out.log'
$Ide211   = $env:BSL_IDE_PATH   # optional; only required by the 'test' action

# --- P/Invoke: suspend / resume via ntdll ---
if (-not ([System.Management.Automation.PSTypeName]'NtProc').Type) {
  Add-Type -Name 'NtProc' -Namespace 'Bsl' -MemberDefinition @'
[DllImport("ntdll.dll", SetLastError=true)]  public static extern int  NtSuspendProcess(System.IntPtr h);
[DllImport("ntdll.dll", SetLastError=true)]  public static extern int  NtResumeProcess(System.IntPtr h);
[DllImport("kernel32.dll", SetLastError=true)] public static extern System.IntPtr OpenProcess(int access, bool inherit, int pid);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(System.IntPtr h);
'@
}
$PROCESS_SUSPEND_RESUME = 0x0800

function Get-NineRouterPid {
  $c = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { return [int]$c.OwningProcess }
  return $null
}

function Invoke-Freeze([int]$procId, [bool]$suspend) {
  $h = [Bsl.NtProc]::OpenProcess($PROCESS_SUSPEND_RESUME, $false, $procId)
  if ($h -eq [IntPtr]::Zero) {
    throw "OpenProcess failed for PID $procId (need same-user or admin rights). Win32=$([System.Runtime.InteropServices.Marshal]::GetLastWin32Error())"
  }
  try {
    if ($suspend) { [Bsl.NtProc]::NtSuspendProcess($h) | Out-Null }
    else          { [Bsl.NtProc]::NtResumeProcess($h)  | Out-Null }
  } finally {
    [Bsl.NtProc]::CloseHandle($h) | Out-Null
  }
}

function Get-InfCount {
  if (-not (Test-Path $BslLog)) { return 0 }
  (Get-Content $BslLog -ErrorAction SilentlyContinue | Select-String ':generateContent|:streamGenerateContent').Count
}

function Get-Ls211 {
  Get-CimInstance Win32_Process -Filter "Name='language_server_windows_x64.exe'" |
    Where-Object { $_.CommandLine -match '6969' }
}

function Show-Status {
  $rpid = Get-NineRouterPid
  $owner = if ($rpid) { (Get-CimInstance Win32_Process -Filter "ProcessId=$rpid").Name } else { '(none)' }
  Write-Host "9router :443 owner : PID=$rpid ($owner)"
  $ls = @(Get-Ls211)
  Write-Host "2.1.1 LS(:6969)    : $($ls.Count) process(es)"
  foreach ($p in $ls) {
    $conn = Get-NetTCPConnection -OwningProcess $p.ProcessId -ErrorAction SilentlyContinue
    $b = @($conn | Where-Object { $_.RemotePort -eq 6969 }).Count
    $r = @($conn | Where-Object { $_.RemotePort -eq 443 -and $_.RemoteAddress -in '127.0.0.1','::1' }).Count
    Write-Host "   PID=$($p.ProcessId)  ->BSL:6969=$b  ->9router:443=$r"
  }
  Write-Host "BSL inference count: $(Get-InfCount)"
}

switch ($Action) {

  'isolate' {
    $rpid = Get-NineRouterPid
    if (-not $rpid) { Write-Host 'ISOLATE: no :443 listener found — nothing to freeze.'; break }
    Invoke-Freeze $rpid $true
    Write-Host "ISOLATE: 9router PID=$rpid SUSPENDED. :443 is frozen. (this chat will pause)"
    Write-Host "Release with:  powershell -File `"$PSCommandPath`" -Action release"
  }

  'release' {
    $rpid = Get-NineRouterPid
    if (-not $rpid) { Write-Host 'RELEASE: no :443 listener PID found.'; break }
    Invoke-Freeze $rpid $false
    Write-Host "RELEASE: 9router PID=$rpid RESUMED. :443 serving again. (chat restored)"
  }

  'status' { Show-Status }

  'test' {
    Write-Host '=== BSL<->9router ISOLATION PROBE ==='

    # 1) Ensure 2.1.1 language server (:6969) is alive; relaunch if needed.
    if (-not (@(Get-Ls211).Count)) {
      Write-Host '2.1.1 LS not found -> launching 2.1.1...'
      Start-Process -FilePath $Ide211
      for ($w = 0; $w -lt 75; $w += 5) {
        Start-Sleep -Seconds 5
        if (@(Get-Ls211).Count) { break }
      }
    }
    $ls = @(Get-Ls211)
    if (-not $ls.Count) { Write-Host 'FATAL: 2.1.1 LS(:6969) never came up. Aborting (9router untouched).'; break }
    Write-Host "2.1.1 LS ready: PID(s) $([string]::Join(',', ($ls | ForEach-Object { $_.ProcessId })))"

    $baseline = Get-InfCount
    Write-Host "BSL inference baseline: $baseline"

    $rpid = Get-NineRouterPid
    if (-not $rpid) { Write-Host 'FATAL: no :443 listener to isolate.'; break }

    # Small delay so the launching shell can detach cleanly before the freeze.
    Start-Sleep -Seconds 8

    $detected = $false
    $sawBsl = 0; $saw9r = 0; $sawExt = 0
    try {
      Invoke-Freeze $rpid $true
      Write-Host "ISOLATION ON  @ $(Get-Date -Format HH:mm:ss)  (9router PID=$rpid frozen)"
      Write-Host '>>> GO NOW: in the "BSL Router - Antigravity IDE" window, pick Gemini 3.1 Pro (High) and send: hello'
      Write-Host ">>> (this chat is paused during the ${WindowSeconds}s window; it auto-recovers after)"

      for ($t = 0; $t -lt $WindowSeconds; $t += 3) {
        $now = Get-InfCount
        foreach ($p in @(Get-Ls211)) {
          $conn = Get-NetTCPConnection -OwningProcess $p.ProcessId -ErrorAction SilentlyContinue
          $sawBsl += @($conn | Where-Object { $_.RemotePort -eq 6969 }).Count
          $saw9r  += @($conn | Where-Object { $_.RemotePort -eq 443 -and $_.RemoteAddress -in '127.0.0.1','::1' }).Count
          $sawExt += @($conn | Where-Object { $_.RemotePort -eq 443 -and $_.RemoteAddress -notin '127.0.0.1','::1' }).Count
        }
        if ($now -gt $baseline) { $detected = $true; Write-Host "*** BSL INFERENCE DETECTED @ $(Get-Date -Format HH:mm:ss) (count $baseline -> $now) ***"; break }
        Start-Sleep -Seconds 3
      }
    }
    finally {
      Invoke-Freeze $rpid $false
      Write-Host "ISOLATION OFF @ $(Get-Date -Format HH:mm:ss)  (9router PID=$rpid resumed)"
    }

    Write-Host ''
    Write-Host '=== VERDICT ==='
    $final = Get-InfCount
    Write-Host "BSL inference: $baseline -> $final  (delta $($final - $baseline))"
    Write-Host "2.1.1 socket hits during window:  BSL:6969=$sawBsl   9router:443=$saw9r   extGoogle:443=$sawExt"
    if ($detected) {
      Write-Host 'RESULT: SUCCESS — 2.1.1 inference reached BSL while 9router was frozen. B works.'
    } elseif ($saw9r -gt 0) {
      Write-Host 'RESULT: 2.1.1 tried 9router:443 (hostname fallback) while frozen => IDE is NOT honoring :6969 for inference. Root cause = IDE inference URL, not 9router.'
    } else {
      Write-Host 'RESULT: no inference seen. Either no message was sent in the 2.1.1 window, or 2.1.1 went to extGoogle:443. See socket hits above.'
    }
    Write-Host ''
    Write-Host '--- last BSL non-admin log lines ---'
    Get-Content $BslLog -Tail 40 -ErrorAction SilentlyContinue |
      Where-Object { $_ -notmatch '/api/observability|/api/error-prevention|/admin/' } |
      Select-Object -Last 12 | ForEach-Object { "  $_" }
  }
}
