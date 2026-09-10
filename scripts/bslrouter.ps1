<#
    BSL Router unified launcher.

    One command, runnable from anywhere (after `bslrouter install`), to control
    the FastAPI app server (:6969), with opt-in MITM interceptor (:443) control.

    Usage:
        bslrouter                 Start the app server only (default)
        bslrouter -Background     Start the app server only, hidden, zero windows
        bslrouter start -Mitm     Start the MITM interceptor explicitly
        bslrouter start -App      Start only the app server (explicit)
        bslrouter stop            Stop the app server only (default)
        bslrouter stop -Mitm      Stop every listener on the MITM port
        bslrouter restart         Restart the app server (default)
        bslrouter status          Show what is currently listening
        bslrouter install         Register `bslrouter` on PATH so it runs from anywhere
        bslrouter uninstall       Remove the PATH registration
        bslrouter trust-ca        Install BSL's mitmproxy CA for IDE trust

    Notes:
      * DEFAULT = APP ONLY (2026-08-29 user directive): MITM (Antigravity
        Integration) is started/stopped ONLY from the Admin UI buttons
        (/api/mitm/start + /api/mitm/stop), never auto-started by the launcher.
        This prevents the launcher from fighting the UI buttons over :443.
      * Start is idempotent: a healthy app (or, for -Mitm, a healthy BSL MITM)
        already listening makes start a no-op (use -ForceKill to force).
      * The MITM interceptor binds port 443. Windows normally allows a
        non-elevated process to bind it (no explicit port reservation); if the
        bind is refused for elevation reasons the MITM start fails loudly
        rather than silently routing the IDE straight to Google.
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
        # PID <= 4 is the System process (HTTP.sys binds :443 as PID 4).
        # taskkill on it bugchecks Windows (CRITICAL_PROCESS_DIED, 0xEF) --
        # parity with app/utils/mitm_kill.py:_get_listener_pids (audit 2026-08-31).
        Where-Object { $_ -and [int]$_ -gt 4 } |
        ForEach-Object { [int]$_ })
}

# ── Critical-process guard (0xEF prevention, 2026-08-31) ─────────────────────
# Killing any of these names terminates Windows itself (CRITICAL_PROCESS_DIED
# bugcheck 0xEF) or the entire desktop/service group. Every tree-kill site in
# this launcher checks this list first and REFUSES loudly instead of taking
# the machine down. Mirror of app/utils/mitm_kill.py:_CRITICAL_PROCESS_NAMES.
$CriticalProcessNames = @(
    'system','smss','csrss','wininit','services','lsass','winlogon',
    'svchost','dwm','explorer','conhost','dllhost','runtimebroker',
    'searchhost','sihost','taskhostw','fontdrvhost','spoolsv','audiodg','wmiprvse'
)

function Test-CriticalProcess ([int]$targetPid) {
    if ($targetPid -le 4) { return $true }   # PID 0/4 (System) -- never killable
    try {
        $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    } catch {
        return $true   # cannot identify -> fail closed, never kill blind
    }
    if (-not $proc) { return $false }        # already gone -> taskkill will no-op
    return ($CriticalProcessNames -contains [string]$proc.ProcessName.ToLower())
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

function Get-AppSupervisorRoot ([int]$listenerPid) {
    # APP SUPERVISOR ROOT WALK (audit F3, 2026-08-29): with
    # config.watchdog.auto_restart: true every spawn is a
    # supervisor(parent)+server(child) lineage where BOTH processes share the
    # SAME command line (`python -m app.dualstack_serve --port 6969`). A plain
    # listener kill only nukes the child, so the supervisor respawns it
    # instantly and `stop` is a no-op while `restart` serves STALE build/config.
    #
    # Walk UP the parent chain while the parent is a python/pythonw host whose
    # CommandLine is the BSL dualstack_serve entrypoint AND whose ExecutablePath
    # resolves under the project $Root. That proves the parent is the BSL
    # supervisor (not an unrelated desktop/shell python), so killing it first
    # tears down the whole tree before the child can be re-spawned.
    #
    # NOTE: this is the APP family only. Get-ListenerTreeRoot handles the MITM
    # port (node.exe / mitmdump / app\mitm.py) -- do NOT merge the two; they
    # walk different process families with different evidence rules.
    $rootPid = $listenerPid
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    while ($current -and $current.ParentProcessId -gt 0) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($current.ParentProcessId)" -ErrorAction SilentlyContinue
        if (-not $parent) { break }
        # Resolve the parent's executable under the project root so we never
        # walk past a BSL supervisor into an unrelated python parent.
        $parentPath = [string]$parent.ExecutablePath
        $parentUnderRoot = $false
        if ($parentPath) {
            try { $parentUnderRoot = (Resolve-Path -Path $parentPath -ErrorAction SilentlyContinue).Path -like "$Root*" } catch { $parentUnderRoot = $false }
        }
        $isPythonHost = $parent.Name -match '^(python|python\.exe|pythonw|pythonw\.exe)$'
        $isDualstackCmd = [string]$parent.CommandLine -like '*app.dualstack_serve*'
        if (-not ($isPythonHost -and $isDualstackCmd -and $parentUnderRoot)) { break }
        $rootPid = [int]$parent.ProcessId
        $current = $parent
    }
    return $rootPid
}

function Stop-Tree ([int]$rootPid, [string]$label) {
    if (Test-CriticalProcess $rootPid) {
        Write-StageError 'critical_process_blocked' "Refusing to stop $label (PID $rootPid): Windows-critical process. Aborting to prevent system crash (0xEF)."
        return $false
    }
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
        # CRITICAL-PROCESS PRE-SCAN (0xEF prevention): if ANY owner is
        # Windows-critical (or unidentifiable), abort the entire eviction
        # before the first taskkill -- a partial kill that ends in a bugcheck
        # is worse than a refused one.
        foreach ($listenerPid in $currentPids) {
            if (Test-CriticalProcess $listenerPid) {
                Write-StageError 'critical_process_blocked' "Refusing to kill PID $listenerPid on :$p - Windows-critical or unidentifiable process. Aborting eviction to prevent system crash (0xEF)."
                return $false
            }
        }
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
    #
    # IDEMPOTENCE GATE (2026-08-27 restart-loop postmortem): agents kept
    # "repairing" a HEALTHY router by running `bslrouter start`, which killed
    # the live listener and respawned -- two agents doing this in a loop caused
    # the endless restarts. Now `start` first asks the router if it is alive;
    # a healthy router makes start a NO-OP unless -ForceKill is passed.
    $stalePids = @(Get-ListenerPids $Port)
    if ($stalePids.Count -gt 0 -and -not $ForceKill) {
        # DUAL-STACK HEALTH PROBE (audit F4, 2026-08-29): after a network-stack
        # hiccup the IPv4 accept path can be the dead one while IPv6 still
        # serves fine. A single 127.0.0.1 probe would then falsely declare a
        # healthy router stale and kill it. Probe BOTH stacks -- EITHER
        # returning 200 means the router is alive; only a TOTAL dual-stack
        # blackout counts as stale.
        #
        # PER-STACK ISOLATION (2026-08-31): each probe now gets its OWN
        # try/catch. Invoke-WebRequest raises a TERMINATING error when a
        # connection is refused, so the previous single shared try{} meant a
        # dead IPv4 stack aborted the block BEFORE the IPv6 probe ever ran --
        # the "dual-stack" probe could only ever report what IPv4 said, and a
        # v6-only-healthy router was still killed as stale. That is the exact
        # failure this gate was written to prevent.
        #
        # UNAUTHENTICATED PROBE (2026-08-31): this used to GET /v1/models with a
        # hardcoded Bearer key that was a TYPO of the real one -- one dropped
        # character (38 chars vs the real 39) -- shipped by 01e1643, the very
        # commit that added this gate. It only ever passed because /v1/models
        # does not enforce auth; the moment it does, the probe 401s, $healthy
        # goes $false, and the launcher kills a HEALTHY router. /health needs
        # no credential, so the launcher also stops embedding a live API key
        # in a git-tracked file (config.yaml is gitignored -- this script was
        # the only committed copy of that key).
        $healthy = $false
        foreach ($probe in @("http://127.0.0.1:$Port/health", "http://[::1]:$Port/health")) {
            if ($healthy) { break }
            try {
                $resp = Invoke-WebRequest -Uri $probe -UseBasicParsing -TimeoutSec 4
                if ($resp -and $resp.StatusCode -eq 200) { $healthy = $true }
            } catch {
                # This stack is down. The other one still gets its own chance.
            }
        }
        if ($healthy) {
            # ESCAPE FIX (2026-08-31): the backticks around `restart` were bare,
            # and in a PowerShell double-quoted string a backtick is the ESCAPE
            # character -- "`r" emitted a carriage return, so this printed as
            # "Use -ForceKill or <CR>estart` to force." (observed live). Doubled
            # backticks emit a literal one.
            Write-Ok "Router already healthy on :$Port (PID $($stalePids[0])). Start is a no-op. Use -ForceKill or ``restart`` to force."
            return
        }
        Write-Warn "Listener on :$Port failed health check -- treating as stale."
    }
    if ($stalePids.Count -gt 0) {
        Write-Warn "Stale listener(s) on :$Port -- killing before restart: $(Format-ListenerOwners $stalePids)"
        foreach ($stalePid in $stalePids) {
            if (Test-CriticalProcess $stalePid) {
                Write-StageError 'critical_process_blocked' "Refusing to kill PID $stalePid on :$Port - Windows-critical process. Cannot start app."
                return
            }
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
        # DUAL-STACK FIX (2026-08-27): uvicorn --host 0.0.0.0 is IPv4-only while
        # Windows resolves localhost -> ::1 first, so IPv6-first clients (Node
        # fetch) got instant ECONNREFUSED that looked like transient restarts.
        # The app.dualstack_serve module binds [::] with IPV6_V6ONLY=0 so ONE
        # socket serves both families. Never bind a single family here again.
        $pyExe = Join-Path $VenvBin 'python.exe'
        # JOB-OBJECT ESCAPE (audit F2, 2026-08-29): the old Start-Process +
        # -RedirectStandard* spawned the child INSIDE the caller's Job Object,
        # so a background/agent caller that finished tree-killed the healthy
        # router (today: silently killed it 2 minutes after restore).
        # Win32_Process.Create parents the child under WmiPrvSE -- OUTSIDE any
        # caller job -- so it survives the caller's exit. cmd /c supplies the
        # log redirection WMI lacks; the python path and both log paths are
        # quoted (the project root contains a space, "BSL Router").
        $cmd = 'cmd /c ""{0}" -m app.dualstack_serve --port {1} >> "{2}" 2>> "{3}""' -f $pyExe, $Port, $out, $err
        # HIDDEN CONSOLE (2026-08-29): Win32_Process.Create always allocates a
        # console for console-subsystem children and the 2-arg form shows it, so
        # every restart popped an empty cmd window whose only content went to
        # app.out.log. Win32_ProcessStartup.ShowWindow = 12 (SW_HIDE) creates
        # that console WITHOUT a visible window. Job-object escape (F2) is
        # unchanged -- the child is still parented under WmiPrvSE, and the
        # cmd /c redirection still works (conhost exists, just hidden).
        # NOTE: the CIM cmdlet form (New-CimInstance -ClientOnly +
        # Invoke-CimMethod) rejects the embedded startup object with
        # "Type mismatch" (0x80041005); the classic [wmiclass] COM form
        # accepts it. Scratch-verified 2026-08-29: MainWindowHandle=0,
        # process alive, output redirection intact.
        $startup = ([wmiclass]"Win32_ProcessStartup").CreateInstance()
        $startup.ShowWindow = 12
        Write-Info "Spawning (job-detached, hidden console): $cmd"
        ([wmiclass]"Win32_Process").Create($cmd, $Root, $startup) | Out-Null
        Write-Ok "App server started (background, dual-stack, job-detached) on :$Port  ->  logs: $out"
    } else {
        # Reload is opt-in via config.server.reload (default OFF). Auto-reload on
        # a production router restarts the worker mid-request and drops in-flight
        # streams -- including the inference stream of an agent routing through
        # this very router. When enabled for dev, exclude high-churn non-source
        # paths (.brain/, scratch/, logs) so only real app/ edits trigger reload.
        $reloadFlag = ''
        if ($ReloadEnabled) {
            $reloadFlag = ''
        }
        $pyExe = Join-Path $VenvBin 'python.exe'
        $inner = '"{0}" -m app.dualstack_serve --port {1}{2}' -f $pyExe, $Port, $reloadFlag
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $inner -WorkingDirectory $Root
        Write-Ok ("App server launched (window, dual-stack) on :$Port")
    }
}


function Start-Mitm {
    if (-not (Test-Path $Mitmdump)) { Write-StageError 'launch_failed' "mitmdump not found at $Mitmdump."; exit 1 }
    # Windows does not reserve 443 for elevated processes; a normal user bind
    # succeeds (the vbs-era MITM ran non-elevated for months). Only warn -- a
    # hard exit here would make the one-command stack start fail on machines
    # where no explicit reservation exists.
    if ($MitmPort -eq 443 -and -not (Test-IsAdmin)) {
        Write-Info "Note: not elevated. Binding :443 usually works without admin; continuing."
    }

    # IDEMPOTENCE GATE (2026-08-29): a healthy BSL-owned mitmdump already on
    # :$MitmPort makes start a no-op, mirroring the app gate. This lets
    # `bslrouter -Background` be the one re-runnable command for the whole
    # stack (run it after a MITM crash to bring only the dead half back).
    $currentOwners = @(Get-ListenerPids $MitmPort)
    if ($currentOwners.Count -gt 0) {
        $bslOwners = @($currentOwners | Where-Object { Test-BslMitmOwner $_ })
        if ($bslOwners.Count -eq $currentOwners.Count -and -not $ForceKill) {
            Write-Ok "MITM already running on :$MitmPort ($(Format-ListenerOwners $bslOwners)). Start is a no-op. Use -ForceKill to restart."
            return
        }
    }
    New-Item -ItemType Directory -Force $LogDir | Out-Null

    # Start is authoritative for BSL's OWN listeners: a stale/dead BSL mitmdump
    # tree is always cleared. FOREIGN listeners are different — killing them is
    # only permitted with explicit user consent (-EvictForeign). This is what
    # stops the watchdog from silently stealing :443 back from 9Router every 5s.
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
    # These flags are spelled out inline in both spawn forms below; the old
    # $mitmArgs array they came from was left assigned-but-unreferenced by the
    # WMI/cmd refactor and has been removed so there is exactly ONE definition
    # of the mitmdump argument list per spawn path.

    try {
        if ($Background) {
            $out  = Join-Path $LogDir 'mitm.out.log'
            $merr = Join-Path $LogDir 'mitm.err.log'
            # JOB-OBJECT ESCAPE + HIDDEN (2026-08-29): same pattern as the app
            # spawn below -- [wmiclass] Win32_Process.Create with ShowWindow=12
            # (SW_HIDE) parents mitmdump under WmiPrvSE (survives caller exit,
            # no visible window) while cmd /c supplies the redirection. The old
            # Start-Process + -RedirectStandard* form died with the caller's
            # job object AND showed a console.
            $mitmCmd = 'cmd /c ""{0}" -s app\mitm.py -p {1} --set connection_strategy=lazy --set upstream_cert=false >> "{2}" 2>> "{3}""' -f $Mitmdump, $MitmPort, $out, $merr
            $startup = ([wmiclass]'Win32_ProcessStartup').CreateInstance()
            $startup.ShowWindow = 12
            Write-Info "Spawning MITM (job-detached, hidden console) on :$MitmPort"
            ([wmiclass]'Win32_Process').Create($mitmCmd, $Root, $startup) | Out-Null
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
    # DEFAULT = APP ONLY (2026-08-29 user directive, REVISED): MITM is
    # controlled exclusively by the Admin UI Start/Stop Integration buttons —
    # NOT by this launcher. Auto-starting MITM here would fight the buttons
    # (user clicks Stop, launcher's next run brings it back) and recreate the
    # rogue-respawn behavior this repo just purged. -Mitm remains available
    # for explicit, intentional use.
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
            foreach ($listenerPid in $appPids) {
                # SUPERVISOR-AWARE STOP (audit F3, 2026-08-29): a supervised
                # router is a supervisor(parent)+server(child) pair sharing the
                # same command line. Kill the topmost supervisor FIRST
                # (taskkill /F /T) so the whole tree -- including the
                # supervisor -- is reaped before it can respawn the child. The
                # listener PID is then killed as belt-and-suspenders. Without
                # killing the supervisor first, stop was a no-op (stale
                # restart) while the child kept serving.
                $rootPid = Get-AppSupervisorRoot $listenerPid
                Write-Info "App stop on :$Port - supervisor root PID=$rootPid (listener PID=$listenerPid)"
                if (Test-CriticalProcess $rootPid) {
                    Write-StageError 'critical_process_blocked' "Refusing to kill supervisor root PID $rootPid on :$Port - Windows-critical process. Aborting stop to prevent system crash (0xEF)."
                    exit 1
                }
                & taskkill /F /T /PID $rootPid 2>&1 | Out-Null
                if ($rootPid -ne $listenerPid) {
                    if (Test-CriticalProcess $listenerPid) {
                        Write-StageError 'critical_process_blocked' "Refusing to kill listener PID $listenerPid on :$Port - Windows-critical process."
                        exit 1
                    }
                    & taskkill /F /T /PID $listenerPid 2>&1 | Out-Null
                }
                Start-Sleep -Milliseconds 300
            }
            if (-not (Wait-PortEmpty $Port)) { exit 1 }
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
