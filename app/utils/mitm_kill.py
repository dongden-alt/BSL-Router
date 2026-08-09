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
