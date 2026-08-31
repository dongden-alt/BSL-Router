"""Focused tests for authoritative, verified MITM lifecycle control."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main
import app.utils.mitm_kill as mitm_kill_mod


def _body(response):
    return json.loads(response.body)


def _runtime(*owners, inspection_error=None, port=443):
    owners = list(owners)
    if inspection_error:
        return {
            "state": "unknown", "inspection_error": inspection_error, "server": False,
            "port_occupied": None, "owners": [], "conflict": None, "port": port,
        }
    server = any(owner["is_bsl_mitm"] for owner in owners)
    return {
        "state": "running" if server else ("occupied" if owners else "stopped"),
        "inspection_error": None,
        "server": server,
        "port_occupied": bool(owners),
        "owners": owners,
        "conflict": bool(owners) and (not server or any(not owner["is_bsl_mitm"] for owner in owners)),
        "port": port,
    }


FOREIGN = {"pid": 91, "name": "node.exe", "parent_pid": None, "parent_chain": [], "is_bsl_mitm": False}
BSL = {"pid": 120, "name": "mitmdump.exe", "parent_pid": None, "parent_chain": [], "is_bsl_mitm": True}


def _launcher_ok(*_args):
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def _launcher_failure(code):
    return SimpleNamespace(returncode=1, stdout=f"ERROR[{code}] simulated failure", stderr="")


def setup_function():
    main._MITM_SUPERVISOR.update({
        "desired_running": False,
        "tracked_pid": None,
        "last_state": None,
        "events": [],
    })


def test_node_only_owner_is_non_bsl_conflict(monkeypatch):
    monkeypatch.setattr(main, "_listener_owners", lambda port: [
        main._classify_mitm_owner({"ProcessId": 91, "Name": "node.exe", "CommandLine": "node 9router"})
    ])

    status = main._mitm_runtime_status(443)

    assert status["state"] == "occupied"
    assert status["server"] is False
    assert status["conflict"] is True
    assert status["owners"] == [FOREIGN]


def test_listener_inspection_builds_authoritative_owner_query(monkeypatch):
    captured = {}

    def run(cmd, **kwargs):
        captured["script"] = cmd[-1]
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(main._subprocess, "run", run)

    assert main._listener_owners(443) == []
    assert "Get-NetTCPConnection -State Listen -ErrorAction Stop" in captured["script"]
    assert "Where-Object { $_.LocalPort -eq 443 }" in captured["script"]
    assert "Select-Object -ExpandProperty OwningProcess -Unique" in captured["script"]


def test_inspection_failure_is_explicit_unknown(monkeypatch):
    monkeypatch.setattr(
        main,
        "_listener_owners",
        lambda _port: (_ for _ in ()).throw(RuntimeError("PowerShell inspection unavailable")),
    )

    status = main._mitm_runtime_status(443)

    assert status["state"] == "unknown"
    assert status["inspection_error"] == "PowerShell inspection unavailable"
    assert status["port_occupied"] is None


def test_project_mitmdump_owner_is_bsl():
    # Build the evidence strings from the ACTUAL project root. _classify_mitm_owner
    # attributes a listener to BSL by matching _project_root() against the process
    # command line, so a hardcoded path only passes on the author's machine and
    # fails in every other clone (or a differently-named directory).
    root = main._project_root()
    python_exe = os.path.join(root, ".venv", "Scripts", "python.exe")
    mitmdump_exe = os.path.join(root, ".venv", "Scripts", "mitmdump.exe")
    owner = main._classify_mitm_owner({
        "ProcessId": 120,
        "Name": "python.exe",
        "ExecutablePath": python_exe,
        "CommandLine": "python " + mitmdump_exe + r" -s app\mitm.py -p 443",
    })
    assert owner == BSL | {"name": "python.exe"}


def test_generic_python_without_project_evidence_fails_closed():
    assert main._classify_mitm_owner({"ProcessId": 121, "Name": "python.exe"})["is_bsl_mitm"] is False


def test_elevated_mitmdump_name_is_narrow_fallback():
    assert main._classify_mitm_owner({"ProcessId": 122, "Name": "mitmdump.exe"})["is_bsl_mitm"] is True


def test_mitmdump_with_other_project_evidence_is_not_bsl():
    owner = main._classify_mitm_owner({
        "ProcessId": 123, "Name": "mitmdump.exe",
        "CommandLine": r"C:\OtherRouter\mitmdump.exe -s C:\OtherRouter\mitm.py -p 443",
    })
    assert owner["is_bsl_mitm"] is False


def test_start_authoritatively_cleans_foreign_owner_then_verifies_bsl(monkeypatch):
    calls = []
    statuses = iter([_runtime(FOREIGN), _runtime()])
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: calls.append("inspect") or next(statuses))
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(mitm_kill_mod, "force_kill_mitm_port", lambda port: calls.append(("kill", port)) or (True, "cleared"))
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(("launcher", *args)) or _launcher_ok())
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: calls.append(("verify", expected)) or _runtime(BSL))

    response = asyncio.run(main.mitm_start())
    data = _body(response)

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["server"] is True
    assert data["conflict"] is False
    # New flow: inspect -> pre-kill -> re-inspect -> launch -> verify.
    assert calls == ["inspect", ("kill", 443), "inspect", ("launcher", "start", "-EvictForeign"), ("verify", True)]


def test_start_button_may_evict_foreign_node_and_never_returns_409(monkeypatch):
    """The Start Integration button is an explicit user action, so it MAY evict.

    The consent gate added for the silent-steal fix applies to the WATCHDOG
    (evict_foreign=False), not to this endpoint. Pressing the button must still
    take the port from a foreign proxy in one step -- never a 409, never a
    two-step 'takeover required' modal.
    """
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime(FOREIGN))
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(mitm_kill_mod, "force_kill_mitm_port", lambda port: (True, "cleared"))
    calls = []
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(args) or _launcher_ok())
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: _runtime(BSL))

    response = asyncio.run(main.mitm_start())

    assert response.status_code == 200
    assert _body(response)["server"] is True
    assert calls == [("start", "-EvictForeign")]


def test_start_kill_failure_reports_remaining_owners_and_does_not_verify_launch(monkeypatch):
    """When the pre-kill cannot clear the port, start must fail with kill_failed
    and NEVER run the launcher or the post-launch verification."""
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime(FOREIGN))
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(mitm_kill_mod, "force_kill_mitm_port", lambda port: calls.append(("kill", port)) or (False, "Port still occupied after 3 rounds"))
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(("launcher", *args)) or _launcher_ok())
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: (_ for _ in ()).throw(AssertionError("must not verify a failed kill")))

    response = asyncio.run(main.mitm_start())
    data = _body(response)

    assert response.status_code == 500
    assert data["code"] == "kill_failed"
    assert data["owners"] == [FOREIGN]
    assert calls == [("kill", 443)], "launcher must not run when the pre-kill failed"


def test_start_launch_failure_leaves_runtime_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime())
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(args) or _launcher_failure("launch_failed"))
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: (_ for _ in ()).throw(AssertionError("must not verify a failed launch")))

    response = asyncio.run(main.mitm_start())
    data = _body(response)

    assert response.status_code == 500
    assert data["code"] == "launch_failed"
    assert data["server"] is False
    assert data["port_occupied"] is False
    assert calls == [("start", "-EvictForeign")]


def test_watchdog_path_refuses_foreign_owner_without_launching(monkeypatch):
    """evict_foreign=False must refuse a foreign owner and never call the launcher.

    This is the watchdog's path. Invoking the launcher here would kill whatever
    holds the port, which is exactly the silent-steal bug.
    """
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime(FOREIGN))
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(args) or _launcher_ok())

    response = asyncio.run(main._start_mitm_locked(evict_foreign=False))
    data = _body(response)

    assert response.status_code == 409
    assert data["code"] == "foreign_owner_present"
    assert calls == [], "launcher must not run without consent"


def test_watchdog_path_still_restarts_bsl_when_port_is_free(monkeypatch):
    """Standing down for foreign owners must not break legitimate self-healing."""
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime())
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(args) or _launcher_ok())
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: _runtime(BSL))

    response = asyncio.run(main._start_mitm_locked(evict_foreign=False))

    assert response.status_code == 200
    assert _body(response)["server"] is True
    assert calls == [("start",)], "watchdog must never pass -EvictForeign"


def test_start_reports_admin_required_without_launcher(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime())
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: False)
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *_args: calls.append(True) or _launcher_ok())

    response = asyncio.run(main.mitm_start())

    assert response.status_code == 403
    assert _body(response)["code"] == "admin_required"
    assert calls == []


def test_start_rejects_unverified_post_launch_ownership(monkeypatch):
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime())
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(main, "_run_mitm_launcher", _launcher_ok)
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: _runtime(FOREIGN))

    response = asyncio.run(main.mitm_start())

    assert response.status_code == 500
    assert _body(response)["code"] == "ownership_not_verified"


def test_stop_passes_only_verified_bsl_owner_and_requires_empty_port(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime(BSL))
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *args: calls.append(args) or _launcher_ok())
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: _runtime())

    response = asyncio.run(main.mitm_stop())

    assert response.status_code == 200
    assert _body(response)["port_occupied"] is False
    assert calls == [("stop",)]


def test_stop_refuses_non_bsl_owner_without_killing_it(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime(FOREIGN))
    monkeypatch.setattr(main, "_run_mitm_launcher", lambda *_args: calls.append(True) or _launcher_ok())

    response = asyncio.run(main.mitm_stop())

    assert response.status_code == 500
    assert _body(response)["code"] == "ownership_not_verified"
    assert calls == []


def test_stop_reports_port_not_empty_after_verified_launcher_exit(monkeypatch):
    monkeypatch.setattr(main, "_mitm_runtime_status", lambda: _runtime(BSL))
    monkeypatch.setattr(main, "_mitm_is_admin", lambda: True)
    monkeypatch.setattr(main, "_run_mitm_launcher", _launcher_ok)
    monkeypatch.setattr(main, "_poll_mitm_runtime", lambda expected: _runtime(BSL))

    response = asyncio.run(main.mitm_stop())

    assert response.status_code == 500
    assert _body(response)["code"] == "port_not_empty"


def test_lifecycle_source_has_single_authoritative_start_contract():
    source = Path("app/main.py").read_text(encoding="utf-8")
    launcher = Path("scripts/bslrouter.ps1").read_text(encoding="utf-8")

    assert "_MITM_LIFECYCLE_LOCK = asyncio.Lock()" in source
    assert source.count("async with _MITM_LIFECYCLE_LOCK:") == 3
    assert "/api/mitm/takeover" not in source
    assert "takeover_required" not in source
    assert "-Verb RunAs" not in launcher
    assert "$currentOwners = @(Get-ListenerPids $MitmPort)" in launcher
    assert "Stop-AllListeners $MitmPort 'MITM'" in launcher
    assert "ERROR[kill_failed]" not in launcher
    assert "Write-StageError 'kill_failed'" in launcher
    assert "tempfile.TemporaryFile" in source
    assert "_subprocess.Popen(" in source
    assert "capture_output=True" not in source.split("def _run_mitm_launcher", 1)[1].split("def _poll_mitm_runtime", 1)[0]
    start_function = launcher.split("function Start-Mitm {", 1)[1].split("function Get-ServiceSelection", 1)[0]

    # SPAWN-FORM AGNOSTIC ORDERING (2026-08-31): these assertions used to key on
    # "Start-Process -FilePath $Mitmdump", which no longer exists in Start-Mitm.
    # Background spawn moved to [wmiclass]Win32_Process.Create (job-object escape
    # so an exiting caller cannot tree-kill mitmdump) and window spawn to
    # Start-Process cmd.exe /k. The only remaining Start-Process -FilePath
    # $Mitmdump in the file is in Ensure-BslCaTrust, which briefly runs mitmdump
    # on throwaway port 8083 to materialize the CA -- NOT the real MITM launch.
    # Keying on it made this test raise ValueError instead of verifying anything.
    # Anchor on the log line each spawn path emits: those are inside Start-Mitm,
    # one per spawn form, and survive future changes to the spawn mechanism.
    launch_markers = [
        start_function.index('Write-Info "MITM launch dispatched (background) on :$MitmPort'),
        start_function.index('Write-Info "MITM launch dispatched (window) on :$MitmPort'),
    ]
    first_launch = min(launch_markers)

    # Both spawn forms must exist, and neither may reintroduce a job-object-bound
    # background spawn (the 2026-08-29 F2 audit: Start-Process + -RedirectStandard*
    # died with the caller, silently killing a healthy MITM).
    assert "([wmiclass]'Win32_Process').Create($mitmCmd, $Root, $startup)" in start_function
    assert "Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $inner" in start_function
    assert "-RedirectStandardOutput" not in start_function
    assert "-RedirectStandardError" not in start_function

    # Kill BEFORE launch: a stale BSL tree must be cleared first or the new
    # mitmdump cannot bind the port.
    assert start_function.index("Stop-AllListeners $MitmPort 'MITM'") < first_launch

    # Ownership verification must still happen AFTER launch. This keys on the
    # verification loop's deadline rather than the first Test-BslMitmOwner use,
    # because the consent gate below legitimately calls Test-BslMitmOwner
    # BEFORE launch to classify existing owners as foreign.
    assert max(launch_markers) < start_function.index("$deadline = [DateTime]::UtcNow.AddSeconds(20)")

    # CONSENT GATE: a foreign owner may only be evicted with -EvictForeign, which
    # solely the Start Integration button passes. This is what prevents the
    # watchdog from silently stealing :443 back from another proxy (e.g. 9Router).
    assert "[switch]$EvictForeign" in launcher
    assert "Write-StageError 'foreign_owner_present'" in launcher
    assert "-not $EvictForeign" in start_function
    # The gate must be evaluated BEFORE anything is killed.
    assert start_function.index("-not $EvictForeign") < start_function.index("Stop-AllListeners $MitmPort 'MITM'")
    # Python side: only the button endpoint may pass consent; the watchdog must not.
    assert "await _start_mitm_locked(evict_foreign=True)" in source
    assert "await _start_mitm_locked(evict_foreign=False)" in source
    assert '("-EvictForeign",) if evict_foreign else ()' in source


def _powershell_code_only(script: str) -> str:
    """Strip whole-line PowerShell comments.

    Every "pattern X must not appear" assertion against the launcher has to run
    against CODE, never prose. The comments in bslrouter.ps1 deliberately record
    what was removed and why (the typo'd probe key, /v1/models, $mitmArgs), so a
    bare substring check is matched by the very documentation that explains the
    fix -- a self-defeating assertion. Full-line comments only: trailing-comment
    stripping would need real tokenizing to avoid mangling '#' inside strings.
    """
    return "\n".join(
        line for line in script.splitlines()
        if not line.strip().startswith("#")
    )


def test_launcher_health_gate_is_unauthenticated_and_probes_each_stack_independently():
    """The idempotence gate must not kill a healthy router.

    Two real defects this locks out, both found 2026-08-31:
      1. The probe carried a hardcoded Bearer key that was a TYPO of the real one
         (a dropped 'j'). It passed only because /v1/models does not enforce
         auth; the day it does, the gate 401s and kills a HEALTHY router. It also
         put a live API key in a git-tracked file (config.yaml is gitignored, so
         this script was the only committed copy).
      2. Both probes shared ONE try/catch. Invoke-WebRequest raises a terminating
         error on a refused connection, so a dead IPv4 stack aborted the block
         before the IPv6 probe ran -- the "dual-stack" gate could only ever
         report IPv4, defeating its own purpose.
    """
    launcher = Path("scripts/bslrouter.ps1").read_text(encoding="utf-8")
    launcher_code = _powershell_code_only(launcher)
    start_app = launcher_code.split("function Start-App {", 1)[1].split("function Start-Mitm", 1)[0]

    # No credential in the health gate, and no API key anywhere in the launcher.
    assert "Authorization" not in start_app
    assert "sk-bsl-" not in launcher_code, "launcher must not embed a live API key"

    # Unauthenticated endpoint, both families.
    assert 'http://127.0.0.1:$Port/health' in start_app
    assert 'http://[::1]:$Port/health' in start_app
    assert "/v1/models" not in start_app

    # Per-stack isolation: the probe loop carries its own try/catch INSIDE the
    # foreach, so one dead family cannot suppress the other. Slice the loop body
    # by its own boundaries -- an earlier version split on "if ($healthy) {" and
    # hit the loop's internal `if ($healthy) { break }` guard first, cutting the
    # region off before the try/catch it was meant to inspect.
    probe_block = start_app.split("foreach ($probe in", 1)[1].split("Write-Ok \"Router already healthy", 1)[0]
    assert probe_block.count("try {") == 1, "probe loop must carry exactly one try per iteration"
    assert probe_block.count("} catch {") == 1, "each stack needs its own catch"
    # Shared-try regression guard: the old form probed both stacks in one try{}.
    assert '$v4 = Invoke-WebRequest' not in start_app
    assert '$v6 = Invoke-WebRequest' not in start_app

    # A healthy router must still be a no-op (the anti-restart-loop guarantee).
    assert "Start is a no-op" in start_app


def test_launcher_has_no_dead_mitm_arg_array():
    """$mitmArgs was assigned once and referenced nowhere after the WMI/cmd
    refactor. Two competing definitions of mitmdump's flags is a drift hazard:
    editing the dead array looks like it works and changes nothing."""
    launcher = Path("scripts/bslrouter.ps1").read_text(encoding="utf-8")
    launcher_code = _powershell_code_only(launcher)

    assert "$mitmArgs" not in launcher_code

    # The real flags still reach both spawn paths (background WMI + window cmd).
    assert launcher_code.count("connection_strategy=lazy") == 2
    assert launcher_code.count("upstream_cert=false") == 2


def test_frontend_has_no_takeover_modal_or_endpoint_and_updates_only_after_verified_success():
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    function = source.split("window.toggleMasterMITM = async (enabled) => {", 1)[1].split("async function removeManagedMitmHosts", 1)[0]

    assert "takeover" not in source.lower()
    assert "/api/mitm/takeover" not in source
    assert "data.ok === true" in function
    assert "data.server === enabled" in function
    assert "enabled ? data.conflict === false : data.port_occupied === false" in function
    assert function.index("if (!res.ok || !verified)") < function.index("globalConfig.mitm.enabled = enabled")
    assert "btn.dataset.requestPending = 'true'" in function
    assert "removeManagedMitmHosts" in function


def test_frontend_dns_gate_uses_fresh_authoritative_runtime_without_mutating_master_state():
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    dns_function = source.split("window.toggleIdeDns = async (ide, enabled) => {", 1)[1].split("let mitmStatusPollTimer", 1)[0]
    poll_function = source.split("async function pollMitmStatus() {", 1)[1].split("// TODO 3 (done): Cloudflare Tunnel", 1)[0]

    assert "const runtime = await pollMitmStatus();" in dns_function
    assert "runtime.state === 'owned'" in dns_function
    assert "runtime.ownership_verified === true" in dns_function
    assert "runtime.server === true" in dns_function
    assert "runtime.conflict === false" in dns_function
    assert "runtime.state === 'running'" not in dns_function
    assert "globalConfig.mitm?.enabled" not in dns_function
    assert "globalConfig.mitm.enabled = Boolean(" not in poll_function


def test_passive_supervisor_detects_tracked_pid_replacement():
    owned = main._set_mitm_supervisor_target(True, _runtime(BSL))
    replacement = BSL | {"pid": 999}
    lost = main._reconcile_mitm_runtime(_runtime(replacement))

    assert owned["state"] == "owned"
    assert owned["tracked_pid"] == 120
    assert lost["state"] == "ownership-lost"
    assert lost["ownership_verified"] is False
    assert lost["ownership_lost"] is True
    assert lost["tracked_pid"] == 120


def test_passive_supervisor_reports_foreign_owner_after_verified_start():
    main._set_mitm_supervisor_target(True, _runtime(BSL))
    lost = main._reconcile_mitm_runtime(_runtime(FOREIGN))

    assert lost["state"] == "ownership-lost"
    assert lost["owners"] == [FOREIGN]
    assert lost["lifecycle_events"][-1]["transition"] == "owned->ownership-lost"


def test_passive_supervisor_event_history_is_bounded():
    for index in range(30):
        main._MITM_SUPERVISOR["last_state"] = f"previous-{index}"
        main._reconcile_mitm_runtime(_runtime())

    assert len(main._MITM_SUPERVISOR["events"]) == 20


def test_frontend_continuously_polls_and_displays_owner_chain():
    source = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "setInterval(() =>" in source
    assert "}, 2000);" in source
    # Polling must ALWAYS run — the gate that only polled on the MITM tab must
    # be gone entirely (audit F12: old assertions were vacuous).
    assert "selectedTab" not in source, "tab-gated polling was reintroduced"
    assert "if (activeTab === 'mitm') pollMitmStatus();" not in source
    # Button must key on the reconcile STATE ('owned'), not ownership_verified
    # alone (audit HIGH-1), and show warning states for foreign/lost.
    assert "const isRunning = status.state === 'owned';" in source
    assert "isForeign" in source
    assert "isLost" in source
    assert "&#9888; Start Server" in source
    assert "OWNERSHIP LOST" in source
    assert "owner.parent_chain" in source
    assert "s.ownership_verified" in source
