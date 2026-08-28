"""External process supervisor for BSL Router (B1 REWORK 2026-08-27).

When config.watchdog.auto_restart is True, ``python -m app.dualstack_serve``
delegates here instead of serving directly. This module becomes the PARENT
process and spawns the router as a supervised child via
``python -m app.dualstack_serve`` — the SAME entrypoint, so the dual-stack
socket and bind-retry protections apply under supervision too.

Why the rework (audit 2026-08-27, Gap B/C):
- The old spawn was bare ``uvicorn app.main:app --host 0.0.0.0`` — it
  BYPASSED dualstack_serve (regressing the IPv6 fix) and the old loop
  EXITED when the child exited, so nothing ever respawned a crash.
- The old health probe hit /api/antifreeze/status; the public O(1) /health
  endpoint is equivalent for liveness and needs no auth consideration.

Lifecycle contract:
- Child exit code 0   → graceful path (/api/version/restart relaunches a new
                        detached instance then exits 0; /api/system/shutdown
                        tree-kills this parent via its ancestry walk). The
                        supervisor stands down. NO respawn.
- Child exit != 0     → crash → RESPAWN (crash-loop capped).
- Health frozen 15s   → kill + RESPAWN (event-loop freeze; model errors and
                        stream stalls do NOT trip an O(1) dict-read route).
- Port never opens    → the child may sit in bind-retry for up to 120s
                        waiting for a zombie holder to release :6969; the
                        boot gate waits up to CHILD_BOOT_TIMEOUT_S (150s)
                        BEFORE any health probe, so a mid-retry child is
                        never murdered for "not serving yet" (the parent-side
                        twin of the bind-retry/watchdog race audited 2026-08-27).

Fork-bomb guard: the child is spawned with BSL_SUPERVISED=1 so its own
main() never re-enters run_supervised.

Stopping from outside: `bslrouter stop` kills this parent FIRST (it holds
no socket, so listener-only kills would let it respawn the child forever),
then the listener child.
"""

from __future__ import annotations

import os
import sys
import time
import signal
import socket
import subprocess

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".brain", "logs"
)
_LOG_FILE = os.path.join(_LOG_DIR, "watchdog.log")

# ── Tunables ──────────────────────────────────────────────────────────────
HEALTH_POLL_INTERVAL_S = 5.0
HEALTH_PROBE_TIMEOUT_S = 3.0
MAX_CONSECUTIVE_FAILURES = 3       # 3 × 5s = 15s of silence → restart
MAX_RESTARTS_PER_WINDOW = 5        # anti-crash-loop
RESTART_WINDOW_S = 600.0           # 10 minutes
RESTART_COOLDOWN_S = 2.0           # brief pause between kill and re-spawn
# Must exceed dualstack_serve._BIND_RETRY_TOTAL_S (120s) so a child waiting
# out a zombie port holder is never mistaken for a dead child.
CHILD_BOOT_TIMEOUT_S = 150.0


def _ensure_log_dir() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)


def _log(msg: str) -> None:
    """Write to watchdog.log and stdout (so the parent terminal sees it)."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never kill the watchdog
    try:
        print(line, flush=True)
    except Exception:
        pass  # console encoding (cp1252 redirects) must never kill it either
        # (2026-08-27 live fire: a redirected stdout made the "≤" in a log
        # line raise UnicodeEncodeError and take the whole stack down)


def _health_check(port: int) -> bool:
    """Return True if the router answered /health from EITHER stack.

    KEY FIX 2026-08-29 (split-brain socket, incident 04:20): probing only
    127.0.0.1 declared a router dead while its IPv6 path still served
    ::1 clients fine. A router is down only when BOTH stacks are silent.
    """
    for host in ("127.0.0.1", "[::1]"):
        try:
            import httpx
            url = f"http://{host}:{port}/health"
            resp = httpx.Client(timeout=HEALTH_PROBE_TIMEOUT_S).get(url)
            if resp.status_code == 200:
                return True
        except Exception:
            continue
    return False


def _wait_for_port(port: int, host: str = "127.0.0.1",
                   timeout_s: float = CHILD_BOOT_TIMEOUT_S) -> bool:
    """Block until something accepts TCP on host:port, or timeout.

    This is the boot gate: bind-retry inside the child can legitimately take
    up to 120s (zombie port holder). Health probes must not start — and the
    crash counter must not run — until the port is actually OPEN.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except OSError:
            time.sleep(2.0)
    return False


_CHILD_LOG_REGISTRY: list = []


def _spawn_child(port: int) -> subprocess.Popen:
    """Spawn the router as a supervised ``app.dualstack_serve`` child.

    Child stdout/stderr are captured to .brain/logs/child_{out,err}.log: the
    supervisor runs headless under the scheduler, so without redirection a
    crash traceback would vanish (live complaint 2026-08-27: "router crashes
    while idle" with zero forensic trace anywhere).
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-m", "app.dualstack_serve", "--port", str(port)]
    env = os.environ.copy()
    # Fork-bomb guard: the child must SERVE, never re-enter supervision.
    env["BSL_SUPERVISED"] = "1"
    # Unicode safety: the child's stdout may be a redirected log file that
    # Windows encodes as cp1252 — force UTF-8 stdio so app prints containing
    # non-ASCII can never crash the router child (same class of bug that
    # killed the supervisor itself on 2026-08-27).
    env["PYTHONIOENCODING"] = "utf-8"
    # Unbuffered so tracebacks land in the log the instant they happen,
    # even if the process dies before flushing.
    env["PYTHONUNBUFFERED"] = "1"
    _log(f"Spawning router child: {' '.join(cmd)}")

    # Redirect child console output to append-mode log files. Opening in
    # binary mode sidesteps every text-encoding hazard on the parent side.
    out_fh = err_fh = None
    try:
        sep = ("\n===== child spawn %s pid-unknown =====\n"
               % time.strftime("%Y-%m-%d %H:%M:%S")).encode()
        out_fh = open(os.path.join(_LOG_DIR, "child_out.log"), "ab")
        err_fh = open(os.path.join(_LOG_DIR, "child_err.log"), "ab")
        out_fh.write(sep)
        err_fh.write(sep)
        out_fh.flush()
        err_fh.flush()
        # Replace previous handles (respawn) — old pair gets GC-closed.
        _CHILD_LOG_REGISTRY[:] = [out_fh, err_fh]
    except Exception:
        out_fh = err_fh = None  # headless fallback: no redirection

    child = subprocess.Popen(
        cmd, cwd=root, env=env,
        stdout=out_fh if out_fh is not None else None,
        stderr=err_fh if err_fh is not None else None,
    )
    _log(f"Router child PID={child.pid}")
    return child


def _kill_child(child: subprocess.Popen) -> None:
    """Kill the child process gracefully (SIGTERM → SIGKILL fallback)."""
    if child.poll() is not None:
        _log(f"Child PID={child.pid} already exited (code={child.returncode})")
        return

    _log(f"Terminating child PID={child.pid} (SIGTERM)...")
    try:
        child.terminate()
        child.wait(timeout=10)
        _log(f"Child PID={child.pid} terminated gracefully (code={child.returncode})")
    except subprocess.TimeoutExpired:
        _log(f"Child PID={child.pid} did not exit in 10s — SIGKILL")
        child.kill()
        child.wait(timeout=5)
        _log(f"Child PID={child.pid} killed (code={child.returncode})")
    except Exception as e:
        _log(f"Error killing child PID={child.pid}: {e}")
        try:
            child.kill()
        except Exception:
            pass


def run_supervised(port: int = 6969, host: str = "", reload: bool = False,
                   **_legacy_kwargs) -> None:
    """Supervise the router child: respawn on crash/freeze, stand down on
    clean exit, give up after MAX_RESTARTS_PER_WINDOW crashes per window.

    ``host``/``reload``/extra kwargs are accepted and IGNORED — legacy call
    sites (app/main.py __main__ gate) passed uvicorn options; under B1 the
    child decides its own socket via app.dualstack_serve.
    """
    _ensure_log_dir()
    # Windows console-encoding armor: when stdout is redirected (pipe/log
    # file — supervisor spawned by /api/version/restart or a script) Python
    # defaults to the ANSI codepage, where non-ASCII log chars raise
    # UnicodeEncodeError and would kill this process (live incident
    # 2026-08-27 23:32: the "≤" in the boot-gate line took the stack down
    # before the child was ever spawned). Replace unencodable chars instead.
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    _log("=" * 60)
    _log(f"BSL Router supervisor started (port={port}) - child: -m app.dualstack_serve")
    _log(f"Boot gate: TCP <= {int(CHILD_BOOT_TIMEOUT_S)}s | probe /health every "
         f"{HEALTH_POLL_INTERVAL_S}s | restart after {MAX_CONSECUTIVE_FAILURES} fails | "
         f"cap {MAX_RESTARTS_PER_WINDOW}/{int(RESTART_WINDOW_S)}s")

    child = _spawn_child(port)
    if _wait_for_port(port):
        _log(f"Child serving on :{port}")
    else:
        _log(f"Child did not open :{port} within {int(CHILD_BOOT_TIMEOUT_S)}s "
             f"(bind-retry against a zombie holder?) — continuing to supervise")

    restart_timestamps: list[float] = []
    consecutive_failures = 0
    shutting_down = False

    def _on_parent_signal(signum, frame):
        nonlocal shutting_down
        shutting_down = True
        _log(f"Parent received signal {signum} — shutting down")

    signal.signal(signal.SIGINT, _on_parent_signal)
    try:
        signal.signal(signal.SIGTERM, _on_parent_signal)
    except (ValueError, OSError):
        pass  # not main thread / unsupported platform

    while not shutting_down:
        rc = child.poll()
        if rc is not None:
            if rc == 0:
                # Graceful path: /api/version/restart relaunched a new detached
                # supervisor+child pair before exiting 0, or /api/system/
                # shutdown's ancestry tree-kill is about to reap us. Either
                # way, respawning here would fight the new instance.
                _log("Child exited cleanly (code=0) — supervisor standing down.")
                return
            _log(f"Child CRASHED (code={rc})")
        else:
            if _health_check(port):
                if consecutive_failures:
                    _log(f"Router recovered after {consecutive_failures} failure(s)")
                consecutive_failures = 0
                # Healthy: wait out the poll interval and loop. MUST continue —
                # falling through would kill a healthy child every cycle (bug
                # caught by test_child_crash_triggers_respawn before deploy).
                for _ in range(int(HEALTH_POLL_INTERVAL_S)):
                    if shutting_down:
                        break
                    time.sleep(1.0)
                continue
            else:
                consecutive_failures += 1
                _log(f"Health probe FAILED ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                if consecutive_failures < MAX_CONSECUTIVE_FAILURES:
                    for _ in range(int(HEALTH_POLL_INTERVAL_S)):
                        if shutting_down:
                            break
                        time.sleep(1.0)
                    continue
                _log(f"Router frozen ({MAX_CONSECUTIVE_FAILURES} consecutive failures)")

        # ── Restart path (crash OR freeze) ────────────────────────────────
        now = time.monotonic()
        restart_timestamps = [t for t in restart_timestamps if now - t < RESTART_WINDOW_S]
        if len(restart_timestamps) >= MAX_RESTARTS_PER_WINDOW:
            _log("GIVING UP — too many restarts in the crash-loop window. "
                 "Exiting supervisor. Last child process may still be running.")
            return
        restart_timestamps.append(now)
        consecutive_failures = 0
        _log(f"RESTARTING (attempt {len(restart_timestamps)}/{MAX_RESTARTS_PER_WINDOW})")
        _kill_child(child)
        time.sleep(RESTART_COOLDOWN_S)
        child = _spawn_child(port)
        if _wait_for_port(port):
            _log(f"Child serving on :{port}")
        else:
            _log(f"Child did not open :{port} in {int(CHILD_BOOT_TIMEOUT_S)}s — "
                 f"will re-evaluate next cycle")

        for _ in range(int(HEALTH_POLL_INTERVAL_S)):
            if shutting_down:
                break
            time.sleep(1.0)

    # Clean shutdown path (signal)
    _kill_child(child)
    _log("Supervisor exited cleanly")
