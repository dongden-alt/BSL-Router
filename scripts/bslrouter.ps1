<#
    BSL Router unified launcher.

    One command, runnable from anywhere (after `bslrouter install`), to control
    the FastAPI app server (:6969), with explicit MITM interceptor (:443) control.

    Usage:
        bslrouter                 Start the app server only (default)
        bslrouter -Background     Start the app server only, hidden in the background
        bslrouter start           Same as `bslrouter` (app only)
        bslrouter start -App      Start only the app server
        bslrouter start -Mitm     Start only the MITM interceptor
        bslrouter start -App -Mitm Start both services
        bslrouter stop            Stop only the app server (default)
        bslrouter stop -Mitm      Stop every listener on the MITM port
        bslrouter restart         Restart only the app server (default)
        bslrouter restart -Mitm   Restart only the MITM interceptor
        bslrouter status          Show what is currently listening
        bslrouter install         Register `bslrouter` on PATH so it runs from anywhere
        bslrouter uninstall       Remove the PATH registration

    Notes:
      * Start, stop, and restart never manipulate the MITM port unless -Mitm is
        explicitly supplied. Use -App -Mitm together to control both services.
      * The MITM interceptor binds port 443, which requires Administrator.
        The BSL app/launcher must already be elevated; lifecycle requests fail
        fast without opening a UAC prompt when elevation is unavailable.
      * The app root is fixed to the project folder, so the command works from
        any working directory.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'install', 'uninstall', 'trust-ca')]
    [string]$Action = 'start',

    [switch]$Background,
    [switch]$App,
    [switch]$Mitm,
    [int]$MitmPortOverride = 0,
    [switch]$SkipCaTrust,
    [string]$LogDirectory,
    [switch]$ForceKill,

    # Consent gate for evicting FOREIGN listeners (e.g. 9Router's node.exe).
    # Without this switch, Start-Mitm will REFUSE to kill a non-BSL owner of the
    # MITM port and exits with stage code 'foreign_owner_present'. Only an
    # explicit user action (the Start Integration button) may pass it. The
    # background watchdog must NOT, so it can restart a dead BSL mitmdump but
    # can never silently steal the port back from another proxy.
    [switch]$EvictForeign
)

$ErrorActionPreference = 'Stop'

# --- Project locations (auto-detected, so the command runs from anywhere) -----
# $Root resolves to the repository root by walking up from this script's own
# location (scripts/bslrouter.ps1 -> repo root). This keeps `bslrouter` runnable
# from any working directory WITHOUT hardcoding a machine-specific path, so the
# script works for every clone regardless of where the repo lives.
#
# Override with the BSL_ROUTER_ROOT environment variable if you keep the venv
# or project in a non-standard location.
if ($env:BSL_ROUTER_ROOT) {
    $Root = $env:BSL_ROUTER_ROOT
} else {
    $Root = Split-Path -Parent $PSScriptRoot
}

if (-not (Test-Path (Join-Path $Root 'app\main.py'))) {
    Write-Error @"
Could not locate the BSL Router project root.
  Resolved root: $Root
  Expected file: $(Join-Path $Root 'app\main.py')

Run this script from inside the repository, or set BSL_ROUTER_ROOT:
  `$env:BSL_ROUTER_ROOT = 'C:\path\to\bsl-router'
"@
    exit 1
}

$VenvBin   = Join-Path $Root '.venv\Scripts'
$Uvicorn   = Join-Path $VenvBin 'uvicorn.exe'
$Mitmdump  = Join-Path $VenvBin 'mitmdump.exe'
$LogDir    = Join-Path $Root '.brain\logs'
# PATH shim location for `bslrouter install`. Override with BSL_ROUTER_BIN.
$BinDir    = if ($env:BSL_ROUTER_BIN) { $env:BSL_ROUTER_BIN } else { Join-Path $env:LOCALAPPDATA 'bsl-router\bin' }

# --- Ports (read from config.yaml when possible, else canonical defaults) -----
$Port     = 6969
$MitmPort = 443
# Reload is opt-in via config.server.reload (default OFF), mirroring the
# app/main.py __main__ contract. The launcher invokes uvicorn directly (not
# python -m app.main), so it must re-implement the same gate here or the
# config flag is silently bypassed.
$ReloadEnabled = $false
try {
    $cfg = Get-Content (Join-Path $Root 'config.yaml') -Raw -ErrorAction Stop
    if ($cfg -match '(?m)^\s*port:\s*(\d+)')      { $Port = [int]$Matches[1] }
    if ($cfg -match '(?m)^\s*mitm_port:\s*(\d+)') { $MitmPort = [int]$Matches[1] }
    if ($cfg -match '(?m)^\s*reload:\s*true\b')   { $ReloadEnabled = $true }
} catch { }
if ($MitmPortOverride -gt 0) { $MitmPort = $MitmPortOverride }
if ($LogDirectory) { $LogDir = $LogDirectory }

function Write-Info ($m) { Write-Host "[bslrouter] $m" -ForegroundColor Cyan }
function Write-Ok   ($m) { Write-Host "[bslrouter] $m" -ForegroundColor Green }
function Write-Warn ($m) { Write-Host "[bslrouter] $m" -ForegroundColor Yellow }
function Write-Err  ($m) { Write-Host "[bslrouter] $m" -ForegroundColor Red }

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ListenerPids ([int]$p) {
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $p } |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Where-Object { $_ -and [int]$_ -gt 0 } |
        ForEach-Object { [int]$_ })
}

function Write-StageError ([string]$code, [string]$message) {
    Write-Err "ERROR[$code] $message"
}

function Get-ListenerTreeRoot ([int]$listenerPid) {
    # 9Router starts a nested node.exe tree. Kill the highest contiguous Node
    # ancestor so its launcher cannot respawn the listener, but never walk past
    # that narrow process family into an unrelated shell or desktop parent.
    # BSL's mitmdump wrapper is likewise walked only through project-attributed
    # python/mitmdump ancestors, so Stop kills the verified BSL process tree.
    $rootPid = $listenerPid
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    while ($current -and $current.ParentProcessId -gt 0) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($current.ParentProcessId)" -ErrorAction SilentlyContinue
        if (-not $parent) { break }
        $parentEvidence = "$($parent.ExecutablePath) $($parent.CommandLine)"
        $isNodeAncestor = $parent.Name -match '^(node|node\.exe)$'
        $isBslMitmAncestor = $parent.Name -match '^(python|python\.exe|pythonw|pythonw\.exe|mitmdump|mitmdump\.exe)$' -and
            $parentEvidence -like "*$Root*" -and
            ($parentEvidence -like '*mitmdump*' -or $parentEvidence -like '*app\mitm.py*')
        if (-not $isNodeAncestor -and -not $isBslMitmAncestor) { break }
        $rootPid = [int]$parent.ProcessId
        $current = $parent
    }
    return $rootPid
}

function Stop-Tree ([int]$rootPid, [string]$label) {
    Write-Info "Forcefully stopping $label (listener $rootPid)..."
    # User's direct, aggressive kill strategy
    Stop-Process -Id $rootPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 250
    if (-not (Get-Process -Id $rootPid -ErrorAction SilentlyContinue)) { return $true }
    Write-StageError 'kill_failed' "Could not terminate $label listener PID $rootPid."
    return $false
}

function Wait-PortEmpty ([int]$p, [int]$timeoutSeconds = 10) {
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    do {
        $pids = @(Get-ListenerPids $p)
        if ($pids.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    $remaining = @(Get-ListenerPids $p)
    Write-StageError 'port_not_empty' "Port :$p still has listener(s): $(Format-ListenerOwners $remaining)"
    return $false
}

function Stop-AllListeners ([int]$p, [string]$label) {
    $pids = @(Get-ListenerPids $p)
    if ($pids.Count -eq 0) { Write-Info "No listeners on :$p."; return $true }

    Write-Info "Current $label owners on :${p}: $(Format-ListenerOwners $pids)"

    # Use taskkill /F /T instead of Stop-Process -Force.  NOTE (audit F3):
    # taskkill does NOT bypass Windows integrity levels — an elevated foreign
    # process still needs an elevated caller. The real value over
    # Stop-Process is (1) tree-kill (/T) so a launcher cannot respawn the
    # listener, and (2) the verify+retry loop below that defeats auto-restart
    # supervisors (e.g. 9Router). The Python pre-kill in _start_mitm_locked
    # runs after an admin check, so by the time we get here we are elevated.
    # Verify+retry loop: kill → wait 500ms → recheck → retry up to 3 times.
    for ($round = 1; $round -le 3; $round++) {
        $currentPids = @(Get-ListenerPids $p)
        if ($currentPids.Count -eq 0) {
            if ($round -gt 1) { Write-Info "Port :$p cleared after round $round." }
            return $true
        }
        Write-Info "Round $round/3: killing listeners on :$p - $(Format-ListenerOwners $currentPids)"
        foreach ($listenerPid in $currentPids) {
            & taskkill /F /T /PID $listenerPid 2>&1 | Out-Null
        }
        Start-Sleep -Milliseconds 500
    }

    $remaining = @(Get-ListenerPids $p)
    if ($remaining.Count -gt 0) {
        Write-StageError 'port_not_empty' "Port :$p still has listeners after 3 rounds: $(Format-ListenerOwners $remaining)"
        return $false
    }
    return $true
}

function Test-BslMitmOwner ([int]$listenerPid) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    $name = [string]$proc.Name
    $evidence = "$($proc.ExecutablePath) $($proc.CommandLine)"
    $metadataAvailable = -not [string]::IsNullOrWhiteSpace($evidence)
    $projectEvidence = $evidence -like "*$Root*" -and ($evidence -like '*mitmdump*' -or $evidence -like '*app\mitm.py*')
    return (($name -ieq 'mitmdump.exe') -and ($projectEvidence -or -not $metadataAvailable)) -or
        (($name -ieq 'python.exe' -or $name -ieq 'pythonw.exe') -and $projectEvidence)
}

function Ensure-Prereqs {
    if (-not (Test-Path $Uvicorn))  { Write-Err "uvicorn not found at $Uvicorn - is the venv set up?"; exit 1 }
    if (-not (Test-Path $Mitmdump)) { Write-Warn "mitmdump not found at $Mitmdump - MITM will be skipped." }
    New-Item -ItemType Directory -Force $LogDir | Out-Null
}

# --- CA trust (Option A: BSL owns its OWN trust anchor) -----------------------
# BSL Router signs intercepted TLS with its own mitmproxy CA. For the Antigravity
# IDE to trust those leaf certs (and let chat handshakes through), that CA must be
# (1) installed in the Windows Root store, and (2) pointed to by NODE_EXTRA_CA_CERTS
# (the Electron/Node transport reads this). We do NOT reuse 9Router's CA - BSL is
# a self-contained product and must not depend on another tool's trust artifacts.
function Ensure-BslCaTrust {
    $mitmDir   = Join-Path $env:USERPROFILE '.mitmproxy'
    $caCer     = Join-Path $mitmDir 'mitmproxy-ca-cert.cer'
    $caPem     = Join-Path $mitmDir 'mitmproxy-ca-cert.pem'

    # The CA is generated on mitmdump's first run. If missing, spin mitmdump up
    # briefly on a throwaway port to materialize it, then stop it.
    if (-not (Test-Path $caCer)) {
        if (-not (Test-Path $Mitmdump)) { Write-Warn "mitmdump missing - cannot generate BSL CA."; return }
        Write-Warn "BSL mitmproxy CA not found - generating it (one-time)..."
        $gen = Start-Process -FilePath $Mitmdump -ArgumentList @('-p','8083') -PassThru -WindowStyle Hidden
        for ($i = 0; $i -lt 10 -and -not (Test-Path $caCer); $i++) { Start-Sleep -Milliseconds 500 }
        Stop-Process -Id $gen.Id -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $caCer)) { Write-Err "Failed to generate BSL CA at $caCer."; return }

    # 1. Point NODE_EXTRA_CA_CERTS at BSL's OWN CA (user env var - no admin needed).
    #    This replaces any prior 9Router pointer so BSL stops depending on 9Router.
    $current = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
    if ($current -ne $caPem) {
        if ($current) { Write-Info "NODE_EXTRA_CA_CERTS was: $current (replacing with BSL CA)." }
        [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $caPem, 'User')
        Write-Ok "Set NODE_EXTRA_CA_CERTS -> $caPem  (restart the IDE to apply)."
    } else {
        Write-Info "NODE_EXTRA_CA_CERTS already points at BSL CA."
    }

    # 2. Install BSL's CA into the Windows Root store (requires admin, idempotent).
    $trusted = $false
    try { & certutil -verifystore Root mitmproxy > $null 2>&1; $trusted = ($LASTEXITCODE -eq 0) } catch { $trusted = $false }
    if ($trusted) { Write-Info "BSL CA already trusted in Windows Root store."; return }

    Write-Warn "BSL CA not in Windows Root store - installing..."
    if (Test-IsAdmin) {
        & certutil -addstore -f Root $caCer | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "BSL CA installed into Windows Root store." }
        else { Write-Err "certutil failed to add BSL CA (exit $LASTEXITCODE)." }
    } else {
        Write-Err "Root-store install needs Administrator. Restart BSL Router elevated, then run MITM Start again."
    }
}

# --- Start actions ------------------------------------------------------------
function Start-App {
    # Authoritative startup: any stale process holding :$Port is killed before
    # launching uvicorn. Without this, a zombie from a prior crash or unclean
    # shutdown silently blocks the new server -- port stays occupied, new process
    # fails to bind, admin UI never comes up.
    $stalePids = @(Get-ListenerPids $Port)
    if ($stalePids.Count -gt 0) {
        Write-Warn "Stale listener(s) on :$Port -- killing before restart: $(Format-ListenerOwners $stalePids)"
        foreach ($stalePid in $stalePids) {
            Stop-Process -Id $stalePid -Force -ErrorAction SilentlyContinue
        }
        $cleared = Wait-PortEmpty $Port 8
        if (-not $cleared) {
            Write-StageError 'port_not_freed' "Could not free :$Port after killing stale listener(s). Cannot start app."
            return
        }
        Write-Ok "Port :$Port cleared."
    }
    Ensure-Prereqs
    if ($Background) {
        $out = Join-Path $LogDir 'app.out.log'
        $err = Join-Path $LogDir 'app.err.log'
        $appArgs = @('app.main:app', '--host', '0.0.0.0', '--port', "$Port")
        Start-Process -FilePath $Uvicorn -ArgumentList $appArgs -WorkingDirectory $Root `
            -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
        Write-Ok "App server started (background) on :$Port  ->  logs: $out"
    } else {
        # Reload is opt-in via config.server.reload (default OFF). Auto-reload on
        # a production router restarts the worker mid-request and drops in-flight
        # streams -- including the inference stream of an agent routing through
        # this very router. When enabled for dev, exclude high-churn non-source
        # paths (.brain/, scratch/, logs) so only real app/ edits trigger reload.
        $reloadFlag = ''
        if ($ReloadEnabled) {
            $reloadFlag = ' --reload --reload-exclude ".brain/*" --reload-exclude "scratch/*" --reload-exclude "*.log" --reload-exclude "*.jsonl"'
        }
        $inner = '"{0}" app.main:app --host 0.0.0.0 --port {1}{2}' -f $Uvicorn, $Port, $reloadFlag
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $inner -WorkingDirectory $Root
        Write-Ok ("App server launched (window) on :$Port  (reload=$ReloadEnabled)")
    }
}


function Start-Mitm {
    if (-not (Test-Path $Mitmdump)) { Write-StageError 'launch_failed' "mitmdump not found at $Mitmdump."; exit 1 }
    if ($MitmPort -eq 443 -and -not (Test-IsAdmin)) {
        Write-StageError 'admin_required' 'MITM lifecycle requires BSL Router to run as Administrator; no UAC prompt was opened.'
        exit 1
    }
    New-Item -ItemType Directory -Force $LogDir | Out-Null

    # Start is authoritative for BSL's OWN listeners: a stale/dead BSL mitmdump
    # tree is always cleared. FOREIGN listeners are different — killing them is
    # only permitted with explicit user consent (-EvictForeign). This is what
    # stops the watchdog from silently stealing :443 back from 9Router every 5s.
    $currentOwners = @(Get-ListenerPids $MitmPort)
    if ($currentOwners.Count -gt 0) {
        $foreignOwners = @($currentOwners | Where-Object { -not (Test-BslMitmOwner $_) })
        if ($foreignOwners.Count -gt 0 -and -not $EvictForeign) {
            Write-StageError 'foreign_owner_present' "Port :$MitmPort is held by another process ($(Format-ListenerOwners $foreignOwners)). Refusing to evict it without explicit consent. Press Start Integration to take over."
            exit 1
        }
        if (-not (Stop-AllListeners $MitmPort 'MITM')) { exit 1 }
    }

    if (-not $SkipCaTrust) {
        # Make sure the IDE will trust BSL's own CA before we start intercepting.
        Ensure-BslCaTrust
    }

    # Hosts-file interception maps managed domains to 127.0.0.1. Lazy
    # connection strategy is mandatory: request() must choose BSL Router or
    # the real upstream IP before mitmproxy opens the server-side TLS socket.
    $mitmArgs = @(
        '-s', 'app\mitm.py', '-p', "$MitmPort",
        '--set', 'connection_strategy=lazy',
        '--set', 'upstream_cert=false'
    )

    try {
        if ($Background) {
            $out = Join-Path $LogDir 'mitm.out.log'
            $merr = Join-Path $LogDir 'mitm.err.log'
            Start-Process -FilePath $Mitmdump -ArgumentList $mitmArgs -WorkingDirectory $Root `
                -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $merr | Out-Null
            Write-Info "MITM launch dispatched (background) on :$MitmPort  ->  logs: $out"
        } else {
            $inner = '"{0}" -s app\mitm.py -p {1} --set connection_strategy=lazy --set upstream_cert=false' -f $Mitmdump, $MitmPort
            Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $inner -WorkingDirectory $Root | Out-Null
            Write-Info "MITM launch dispatched (window) on :$MitmPort"
        }
    } catch {
        Write-StageError 'launch_failed' "Could not start mitmdump: $($_.Exception.Message)"
        exit 1
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $owners = @(Get-ListenerPids $MitmPort)
        if ($owners.Count -gt 0 -and @($owners | Where-Object { Test-BslMitmOwner $_ }).Count -eq $owners.Count) {
            Write-Ok "MITM verified running on :$MitmPort ($(Format-ListenerOwners $owners))"
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    $remaining = @(Get-ListenerPids $MitmPort)
    Write-StageError 'ownership_not_verified' "BSL MITM did not obtain exclusive ownership of :$MitmPort. Remaining: $(Format-ListenerOwners $remaining)"
    exit 1
}

function Get-ServiceSelection {
    $defaultAppOnly = -not $App -and -not $Mitm
    [PSCustomObject]@{
        App  = [bool]($App -or $defaultAppOnly)
        Mitm = [bool]$Mitm
    }
}

function Invoke-Start {
    $selection = Get-ServiceSelection
    if ($selection.App)  { Start-App }
    if ($selection.Mitm) { Start-Mitm }
    Write-Ok "Done. Admin UI:  http://localhost:$Port/admin/"
}

function Invoke-Stop {
    $selection = Get-ServiceSelection
    if ($selection.App) {
        $appPids = @(Get-ListenerPids $Port)
        if ($appPids.Count -gt 0) {
            foreach ($listenerPid in $appPids) { Stop-Tree $listenerPid "app (:$Port)" }
        } else { Write-Info "App not running on :$Port." }
    }
    if ($selection.Mitm) {
        $currentOwners = @(Get-ListenerPids $MitmPort)
        if ($currentOwners.Count -gt 0) {
            $unverified = @($currentOwners | Where-Object { -not (Test-BslMitmOwner $_) })
            if ($unverified.Count -gt 0 -and -not $ForceKill) {
                Write-StageError 'ownership_not_verified' "Refusing to stop non-BSL MITM owner(s): $(Format-ListenerOwners $unverified)"
                exit 1
            }
            foreach ($listenerPid in $currentOwners) {
                if (-not (Stop-Tree $listenerPid "BSL MITM (:$MitmPort)")) { exit 1 }
            }
            if (-not (Wait-PortEmpty $MitmPort)) { exit 1 }
        }
    }
    if ($selection.Mitm) { Write-Ok "Stop complete. Port :$MitmPort is free." }
    else { Write-Ok "Stop complete. App lifecycle did not manipulate :$MitmPort." }
}

function Format-ListenerOwners ([int[]]$pids) {
    $labels = foreach ($listenerPid in $pids) {
        $proc = Get-Process -Id $listenerPid -ErrorAction SilentlyContinue
        if ($proc) { "$listenerPid/$($proc.ProcessName)" } else { "$listenerPid/unknown" }
    }
    $labels -join ', '
}

function Invoke-Status {
    $appPids = @(Get-ListenerPids $Port)
    $mitmPids = @(Get-ListenerPids $MitmPort)
    Write-Host ""
    Write-Host "  BSL Router status" -ForegroundColor White
    Write-Host "  -----------------" -ForegroundColor DarkGray
    if ($appPids.Count -gt 0) { Write-Ok "  App  :$Port   RUNNING ($(Format-ListenerOwners $appPids))" } else { Write-Warn "  App  :$Port   stopped" }
    if ($mitmPids.Count -gt 0) { Write-Ok "  MITM :$MitmPort    RUNNING ($(Format-ListenerOwners $mitmPids))" } else { Write-Warn "  MITM :$MitmPort    stopped" }
    Write-Host ""
}

function Invoke-Install {
    New-Item -ItemType Directory -Force $BinDir | Out-Null
    $line = 'powershell -NoProfile -ExecutionPolicy Bypass -File "{0}\scripts\bslrouter.ps1" %*' -f $Root
    $shim = "@echo off`r`n" + $line + "`r`n"
    Set-Content -Path (Join-Path $BinDir 'bslrouter.cmd') -Value $shim -Encoding ASCII
    Write-Ok "Shim written: $BinDir\bslrouter.cmd"

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$BinDir*") {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$BinDir", 'User')
        Write-Ok "Added $BinDir to your user PATH."
        Write-Warn "Open a NEW terminal for PATH changes to take effect, then run: bslrouter"
    } else {
        Write-Info "$BinDir already on PATH."
    }
}

function Invoke-Uninstall {
    $shimPath = Join-Path $BinDir 'bslrouter.cmd'
    if (Test-Path $shimPath) { Remove-Item $shimPath -Force; Write-Ok "Removed shim: $shimPath" }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -like "*$BinDir*") {
        $new = ($userPath -split ';' | Where-Object { $_ -and $_ -ne $BinDir }) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $new, 'User')
        Write-Ok "Removed $BinDir from user PATH."
    }
}

switch ($Action) {
    'start'     { Invoke-Start }
    'stop'      { Invoke-Stop }
    'restart'   { Invoke-Stop; Start-Sleep -Seconds 2; Invoke-Start }
    'status'    { Invoke-Status }
    'install'   { Invoke-Install }
    'uninstall' { Invoke-Uninstall }
    'trust-ca'  { Ensure-BslCaTrust }
}
