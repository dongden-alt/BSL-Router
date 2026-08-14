"""External process watchdog for BSL Router.

Launched only when config.watchdog.auto_restart is True. Spawns the router
as a child process via uvicorn, health-polls /api/antifreeze/status
every 5 seconds, and if it fails 3 consecutive times (15s), kills and
restarts the child process.

The health probe is an O(1) dict read — it cannot legitimately stall
unless the asyncio event loop itself is blocked. Model errors, stream
stalls, and high load do NOT trigger a restart.

Design (2026-08-14):
- The watchdog is a PARENT process that spawns the router as a child.
- When enabled, `python -m app.main` delegates here instead of calling
  uvicorn.run() directly.
- Stopping the parent (Ctrl+C / SIGTERM) kills the child cleanly.
- The "Shutdown" button in Settings still works (it calls /api/system/shutdown
  which the child handles normally — the parent just waits for child exit).
- Max restart attempts: 5 within a 10-minute window (anti-crash-loop).
- After max attempts: logs "GIVING UP" and exits.
"""

from __future__ import annotations

import os
import sys
import time
import signal
import subprocess
import logging

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".brain", "logs"
)
_LOG_FILE = os.path.join(_LOG_DIR, "watchdog.log")

# ── Tunables ──────────────────────────────────────────────────────────────
HEALTH_POLL_INTERVAL_S = 5.0
HEALTH_PROBE_TIMEOUT_S = 3.0
MAX_CONSECUTIVE_FAILURES = 3       # 3 × 5s = 15s of silence → restart
MAX_RESTARTS_PER_WINDOW = 5       # anti-crash-loop
RESTART_WINDOW_S = 600.0          # 10 minutes
RESTART_COOLDOWN_S = 2.0          # brief pause between kill and re-spawn


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
    print(line, flush=True)


def _health_check(host: str, port: int) -> bool:
    """Return True if the router answered /api/antifreeze/status with 200."""
    try:
        import httpx
        url = f"http://{host}:{port}/api/antifreeze/status"
        resp = httpx.Client(timeout=HEALTH_PROBE_TIMEOUT_S).get(url)
        return resp.status_code == 200
    except Exception:
        return False


def _spawn_child(host: str, port: int, reload: bool, **uvicorn_kwargs) -> subprocess.Popen:
    """Spawn the router as a child process running uvicorn."""
    cmd = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", host,
        "--port", str(port),
    ]
    if reload:
        cmd.append("--reload")
        excludes = uvicorn_kwargs.get("reload_excludes")
        if excludes:
            for pat in excludes:
                cmd.extend(["--reload-exclude", pat])

    _log(f"Spawning router child: {' '.join(cmd)}")
    child = subprocess.Popen(cmd)
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


def run_supervised(host: str = "0.0.0.0", port: int = 6969, reload: bool = False, **uvicorn_kwargs) -> None:
    """Launch the router as a supervised child process.

    Polls /api/antifreeze/status every 5s. If it fails 3 consecutive times,
    kills and restarts the child. Exits after MAX_RESTARTS_PER_WINDOW crashes
    within a 10-minute window.
    """
    _ensure_log_dir()
    _log("=" * 60)
    _log(f"BSL Router watchdog started (host={host}, port={port}, reload={reload})")
    _log(f"Health probe: every {HEALTH_POLL_INTERVAL_S}s, "
         f"restart after {MAX_CONSECUTIVE_FAILURES} consecutive failures")
    _log(f"Crash-loop protection: max {MAX_RESTARTS_PER_WINDOW} restarts per "
         f"{int(RESTART_WINDOW_S)}s window")

    child = _spawn_child(host, port, reload, **uvicorn_kwargs)
    restart_timestamps: list[float] = []
    consecutive_failures = 0
    shutting_down = False

    def _on_parent_signal(signum, frame):
        nonlocal shutting_down
        shutting_down = True
        _log(f"Parent received signal {signum} — shutting down")
        _kill_child(child)
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_parent_signal)
    signal.signal(signal.SIGTERM, _on_parent_signal)

    # Give the child a moment to boot before first health probe
    time.sleep(3.0)

    while not shutting_down:
        # Check if child exited on its own (e.g. Shutdown button, uvicorn reload)
        rc = child.poll()
        if rc is not None:
            _log(f"Child exited on its own (code={rc}). Watchdog exiting.")
            return

        healthy = _health_check(host, port)

        if healthy:
            if consecutive_failures > 0:
                _log(f"Router recovered after {consecutive_failures} failure(s)")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            _log(f"Health probe FAILED ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Prune old restart timestamps outside the window
                now = time.monotonic()
                restart_timestamps = [t for t in restart_timestamps if now - t < RESTART_WINDOW_S]

                if len(restart_timestamps) >= MAX_RESTARTS_PER_WINDOW:
                    _log("GIVING UP — too many restarts in the crash-loop window. "
                         "Exiting watchdog. Last child process may still be running.")
                    return

                _log(f"Router frozen ({consecutive_failures} consecutive failures) — "
                     f"RESTARTING (attempt {len(restart_timestamps) + 1}/"
                     f"{MAX_RESTARTS_PER_WINDOW})")
                restart_timestamps.append(now)
                consecutive_failures = 0

                _kill_child(child)
                time.sleep(RESTART_COOLDOWN_S)
                child = _spawn_child(host, port, reload, **uvicorn_kwargs)
                # Wait for boot before resuming probes
                time.sleep(5.0)

        # Poll interval (skip if shutting down was triggered during sleep)
        for _ in range(int(HEALTH_POLL_INTERVAL_S)):
            if shutting_down:
                break
            time.sleep(1.0)

    # Clean shutdown path
    _kill_child(child)
    _log("Watchdog exited cleanly")
