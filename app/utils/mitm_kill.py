"""Raw force-kill for ALL processes on the MITM port.

Bypasses the PS1 launcher's ownership verification. Uses netstat + taskkill
directly with a verify/retry loop.

ELEVATION LIMITATION (audit F3): taskkill does NOT bypass Windows integrity
levels. Killing an elevated (high-integrity) process from a non-elevated
BSL Router still fails with Access Denied. The real value of taskkill over
Stop-Process here is (1) tree-kill (/T) so a launcher cannot respawn the
listener, and (2) the verify+retry loop that defeats auto-restart supervisors
(e.g. 9Router). The caller (_start_mitm_locked) checks _mitm_is_admin()
BEFORE invoking this module, so in practice BSL Router is elevated.

Called by mitm_stop(force=True) in the main app -- the first line of defense
before any "is the BSL process verified?" logic runs.

Also called by _start_mitm_locked before launching mitmdump, to clear any
foreign process (e.g. 9Router node.exe) that grabbed port 443 while BSL
was down.
"""

import os
import subprocess
import logging
import time

logger = logging.getLogger("bsl.mitm_kill")

_MAX_KILL_ROUNDS = 3
_KILL_WAIT_MS = 500  # wait between kill and recheck
_SUPERVISOR_WALK_DEPTH = 6

# Tokens that suggest a process is a MITM listener (its command line mentions
# mitmdump or the listen-port flag).
_MITM_TOKENS = ("mitmdump", "mitm.py", "listen-port", "--listen", "listen_port")
# Tokens that suggest a process is a respawn supervisor (a loop or sleep).
_RESPAWN_TOKENS = ("while", "Start-Sleep", "for(", "Repeat")

# ── Critical-process guard (0xEF prevention, 2026-08-31) ─────────────────────
# RCA: the 2026-08-31 13:07 bugcheck (CRITICAL_PROCESS_DIED, 0xEF) correlates
# with a MITM port eviction: taskkill on the Windows System process (PID 4,
# which HTTP.sys uses to bind :443) or on another protected/critical process
# terminates the whole machine, not just the listener. Every kill path in this
# module therefore refuses -- loudly -- when the target is on the critical
# list or cannot be identified while live. Eviction of ordinary foreign owners
# (e.g. 9Router's node.exe) is unchanged.
_CRITICAL_PROCESS_NAMES = frozenset({
    # True protected processes: killing any of these bugchecks Windows.
    "system", "smss", "csrss", "wininit", "services", "lsass", "winlogon",
    # Service/desktop hosts: tree-killing them takes down whole service
    # groups or the interactive desktop (and everything parented under it).
    "svchost", "dwm", "explorer", "conhost", "dllhost", "runtimebroker",
    "searchhost", "sihost", "taskhostw", "fontdrvhost", "spoolsv",
    "audiodg", "wmiprvse",
})


def _is_critical_process_name(name: str) -> bool:
    """True if *name* (case-insensitive, ``.exe`` optional) must never be killed."""
    if not name:
        return False
    stem = name.strip().lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return stem in _CRITICAL_PROCESS_NAMES


def _get_process_names(pids) -> dict:
    """One-shot WMI lookup ``{pid: process_name}`` for *pids*.

    Terminated PIDs are simply absent from the result (racing a dying process
    is benign). Raises RuntimeError when the WMI query itself fails — callers
    must treat that as fail-closed and never kill a live process they could
    not identify.
    """
    if not pids:
        return {}
    filt = " OR ".join(f"ProcessId={int(p)}" for p in sorted(pids))
    cmd = [
        "powershell", "-NoProfile", "-Command",
        f"Get-CimInstance Win32_Process -Filter '{filt}' | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.Name)\" }",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"WMI name lookup failed: {(result.stderr or '').strip()[:200]}")
    names = {}
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_s, _, pname = line.partition("|")
        if pid_s.strip().isdigit() and pname.strip():
            names[int(pid_s.strip())] = pname.strip()
    return names


def _get_listener_pids(port: int) -> set:
    """Return set of PIDs that have a LISTENING socket on *port*.

    Uses an EXACT match on the local port (audit F2): a substring check like
    ``":443" in local`` would also match :4430, :4432, :8443, killing the
    wrong listeners -- amplified by the retry loop.
    """
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"netstat exited with {result.returncode}: {result.stderr.strip()[:200]}")
    pids = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 5:
            continue
        # Local address is parts[1]: '0.0.0.0:443' or '[::]:443'.
        local_port = parts[1].rsplit(":", 1)[-1]
        if local_port != str(port):
            continue
        if "LISTENING" not in parts:
            continue
        try:
            pid = int(parts[-1])
            # Exclude PID 0 (idle) and PID 4 (System/HTTP.sys): taskkill can
            # never terminate them, and retrying wastes all rounds (audit LOW-7).
            # Also exclude our own PID: a misconfigured mitm_port must never
            # kill the BSL Router serving this very request (audit F8).
            if pid > 4 and pid != os.getpid():
                pids.add(pid)
        except (ValueError, IndexError):
            continue
    return pids


def _is_protected_pid(pid: int) -> bool:
    """Safety guard: PID 0 (idle) and PID 4 (System/HTTP.sys) can never be
    terminated via taskkill, and our own PID must never be killed — doing so
    would kill the BSL Router process serving the current request.
    """
    return pid <= 4 or pid == os.getpid()


def _get_process_info(pid: int) -> tuple:
    """Return (parent_pid, command_line) for *pid* via WMI CIM query.

    Returns (None, None) if the process cannot be found (already exited) or
    the query fails.
    """
    try:
        import subprocess as _sp
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction SilentlyContinue; "
            f"if ($p) {{ \"$($p.ParentProcessId)|$($p.CommandLine)\" }} else {{ 'NONE|NONE' }}",
        ]
        result = _sp.run(cmd, capture_output=True, text=True, timeout=10)
        out = (result.stdout or "").strip()
        if not out or out == "NONE|NONE":
            return None, None
        parts = out.split("|", 1)
        ppid_str = parts[0].strip()
        cmdline = parts[1] if len(parts) > 1 else ""
        ppid = int(ppid_str) if ppid_str.isdigit() else None
        return ppid, cmdline
    except Exception:
        return None, None


def _is_respawn_supervisor(cmdline: str) -> bool:
    """A supervisor is a process whose command line contains both a MITM
    listener token (it knows about mitmdump / listen-port) AND a respawn
    token (a loop or sleep primitive). This avoids false positives such as
    a one-off `mitmdump --listen-port 443` invocation, which is just a
    normal listener, not a supervisor.
    """
    if not cmdline:
        return False
    lower = cmdline.lower()
    has_mitm = any(t.lower() in lower for t in _MITM_TOKENS)
    has_respawn = any(t.lower() in lower for t in _RESPAWN_TOKENS)
    return has_mitm and has_respawn


def kill_respawn_supervisors(port: int) -> tuple:
    """Walk the parent chain of every listener on *port* and tree-kill any
    ancestor that is a respawn supervisor — i.e. a `while($true){mitmdump ...}`
    or `Start-Sleep` loop that would otherwise survive a child-only kill and
    respawn a fresh listener ~3s later.

    This is the PRE-STEP that defeats the respawn pattern. Without it,
    `taskkill /T` on the child only kills descendants, never the parent loop.

    Returns (ok: bool, detail: str). ok=True if no supervisors were found or
    all were killed successfully.
    """
    try:
        listener_pids = _get_listener_pids(port)
        if not listener_pids:
            return True, f"No listeners on port {port}; nothing to scan for supervisors."

        supervisor_pids = set()
        for pid in listener_pids:
            current = pid
            for _depth in range(_SUPERVISOR_WALK_DEPTH):
                ppid, cmdline = _get_process_info(current)
                if ppid is None:
                    break
                if _is_protected_pid(ppid):
                    break
                if _is_respawn_supervisor(cmdline or ""):
                    supervisor_pids.add(ppid)
                    logger.info(
                        f"[mitm_kill] Supervisor detected on port {port}: "
                        f"PID {ppid} (child of {pid}) — {cmdline[:120]}"
                    )
                    # Stop walking: a supervisor higher up the chain will be
                    # tree-killed along with this one, or we already flagged it.
                    break
                current = ppid

        if not supervisor_pids:
            return True, f"Port {port}: no respawn supervisors found in parent chains."

        # ── CRITICAL-PROCESS PRE-SCAN (0xEF prevention, 2026-08-31) ─────────
        # Identify every flagged supervisor BEFORE killing anything. A single
        # Windows-critical name aborts the whole sweep: taskkill on such a PID
        # bugchecks the machine (CRITICAL_PROCESS_DIED, 0xEF) instead of
        # freeing the port.
        try:
            sup_names = _get_process_names(supervisor_pids)
            sup_lookup_error = None
        except Exception as exc:  # fail closed — never kill blind
            sup_names = {}
            sup_lookup_error = exc

        blocked = []
        for spid in sorted(supervisor_pids):
            sname = sup_names.get(spid)
            if sname is not None and _is_critical_process_name(sname):
                blocked.append(f"{spid}({sname})")
            elif sname is None and sup_lookup_error is not None:
                blocked.append(f"{spid}(name_unresolvable)")
        if blocked:
            detail = (
                f"Refusing to kill Windows-critical/unidentifiable supervisor(s) "
                f"on port {port}: {', '.join(blocked)}"
            )
            logger.error(f"[mitm_kill] {detail}")
            return False, detail

        killed = []
        failed = []
        for spid in sorted(supervisor_pids):
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(spid)],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    killed.append(str(spid))
                else:
                    stderr_lower = (result.stderr or "").lower()
                    if "not found" in stderr_lower or "no such" in stderr_lower:
                        killed.append(str(spid))
                    else:
                        failed.append(f"{spid}({result.stderr.strip()[:80]})")
            except subprocess.TimeoutExpired:
                failed.append(f"{spid}(timeout)")
            except Exception as e:
                failed.append(f"{spid}({e})")

        detail = f"Killed respawn supervisors: {', '.join(killed)}"
        if failed:
            detail += f" | Failed: {', '.join(failed)}"
        logger.info(f"[mitm_kill] kill_respawn_supervisors port {port}: {detail}")
        ok = len(failed) == 0
        return ok, detail

    except Exception as e:
        logger.error(f"[mitm_kill] kill_respawn_supervisors unexpected error on port {port}: {e}")
        return False, str(e)


def force_kill_mitm_port(port: int = 443) -> tuple:
    """Kill ALL listeners on *port* using raw netstat + taskkill /F /T.

    Includes a verify+retry loop: after killing, wait 500ms, recheck the
    port. If processes are still present (or respawned), kill again. Up to
    3 rounds. This handles cases where a watchdog or auto-restart (e.g.
    9Router) respawns the process immediately after the kill.

    Returns (ok: bool, detail: str).
    - ok=True even if nothing was on the port (nothing to kill).
    - ok=True if all rounds succeeded and the port is verified empty.
    - ok=False if a real error prevented the scan/kill, or if processes
      survive all 3 kill rounds.
    """
    round_num = 0
    try:
        all_killed = []
        all_failed = []

        # PRE-STEP: kill any respawn supervisor (e.g. `while($true){mitmdump}`)
        # BEFORE the listener kill loop. Tree-killing a listener child does NOT
        # kill its parent loop, which would respawn a fresh child ~3s later and
        # defeat the retry loop. Killing the supervisor first breaks the cycle.
        sup_ok, sup_detail = kill_respawn_supervisors(port)
        if not sup_ok:
            logger.warning(
                f"[mitm_kill] Supervisor kill did not fully succeed on port {port}: {sup_detail}"
            )
            # Continue anyway — the listener kill loop below may still clear
            # the port even if a supervisor survived.

        for round_num in range(1, _MAX_KILL_ROUNDS + 1):
            pids = _get_listener_pids(port)

            if not pids:
                # Port is clear — verify it stays clear for a brief moment
                # to catch immediate respawns.
                if round_num == 1:
                    return True, f"Port {port} had no listeners."
                # Subsequent round: we killed something earlier, now it's clear.
                detail = f"Killed PIDs: {', '.join(all_killed)} (round {round_num} verified clear)"
                if all_failed:
                    detail += f" | Earlier failures: {', '.join(all_failed)}"
                logger.info(f"[mitm_kill] Port {port}: {detail}")
                return True, detail

            logger.info(
                f"[mitm_kill] Round {round_num}/{_MAX_KILL_ROUNDS}: "
                f"port {port} has listeners: {sorted(pids)}"
            )

            # ── CRITICAL-PROCESS PRE-SCAN (0xEF prevention, 2026-08-31) ─────
            # Identify every listener BEFORE killing anything this round. A
            # single Windows-critical owner (or one we cannot identify) aborts
            # the eviction: taskkill on such a PID bugchecks the machine
            # (CRITICAL_PROCESS_DIED, 0xEF). PID 4 (System/HTTP.sys) never
            # reaches here — _get_listener_pids excludes pid <= 4 — this is
            # the name-level second line of defense.
            try:
                round_names = _get_process_names(pids)
                round_lookup_error = None
            except Exception as exc:  # fail closed — never kill blind
                round_names = {}
                round_lookup_error = exc

            blocked = []
            for pid in sorted(pids):
                pname = round_names.get(pid)
                if pname is not None and _is_critical_process_name(pname):
                    blocked.append(f"{pid}({pname})")
                elif pname is None and round_lookup_error is not None:
                    blocked.append(f"{pid}(name_unresolvable)")
            if blocked:
                detail = (
                    f"Refusing to kill Windows-critical/unidentifiable process(es) "
                    f"on port {port}: {', '.join(blocked)}"
                )
                logger.error(f"[mitm_kill] {detail}")
                return False, detail

            killed_this_round = []
            failed_this_round = []
            for pid in sorted(pids):
                try:
                    result = subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode == 0:
                        killed_this_round.append(str(pid))
                        all_killed.append(str(pid))
                    else:
                        # taskkill can return non-zero for "process not found"
                        # (already dead) — treat as killed if it's gone.
                        stderr_lower = (result.stderr or "").lower()
                        if "not found" in stderr_lower or "no such" in stderr_lower:
                            killed_this_round.append(str(pid))
                            all_killed.append(str(pid))
                        else:
                            failed_this_round.append(
                                f"{pid}({result.stderr.strip()[:80]})"
                            )
                            all_failed.append(f"{pid}({result.stderr.strip()[:80]})")
                except subprocess.TimeoutExpired:
                    failed_this_round.append(f"{pid}(timeout)")
                    all_failed.append(f"{pid}(timeout)")
                except Exception as e:
                    failed_this_round.append(f"{pid}({e})")
                    all_failed.append(f"{pid}({e})")

            # Wait before rechecking — gives the OS time to release the socket
            # and catches immediate respawns.
            time.sleep(_KILL_WAIT_MS / 1000.0)

        # After all rounds, do a final check.
        remaining = _get_listener_pids(port)
        if remaining:
            detail = (
                f"Port {port} still has listeners after {_MAX_KILL_ROUNDS} rounds: "
                f"{sorted(remaining)}. Killed: {', '.join(all_killed)}. "
                f"Failed: {', '.join(all_failed)}."
            )
            logger.error(f"[mitm_kill] {detail}")
            return False, detail

        detail = f"Killed PIDs: {', '.join(all_killed)} (cleared after {round_num} round(s))"
        if all_failed:
            detail += f" | Some failures (non-blocking): {', '.join(all_failed)}"
        logger.info(f"[mitm_kill] Port {port}: {detail}")
        return True, detail

    except subprocess.TimeoutExpired:
        return False, "netstat timed out after 15s"
    except FileNotFoundError:
        # netstat not on PATH (extremely unlikely on Windows Server/Pro)
        return False, "netstat not found on PATH"
    except Exception as e:
        logger.error(f"[mitm_kill] Unexpected error on port {port}: {e}")
        return False, str(e)
