"""
BSL Router Zero-Downtime Pre-Warmed Handover Worker (`app.prewarm_handover`)
============================================================================
Eliminates the 6-7 second `ECONNREFUSED` cold-boot gap when restarting `BSL Router (:6969)`
from the WebUI (`POST /api/system/restart`, `POST /api/version/restart`) or CLI (`bsl-router-ctl.mjs`):
1. Starts a new Python process in the background while the old `:6969` process is STILL alive.
2. Imports `uvicorn`, `app.dualstack_serve`, `app.main`, and runtime bridges (~5.0s) and compiles
   the ASGI lifespan/middleware stack (`config.load()`) while the old process continues serving
   `:6969` with zero interruption.
3. Waits for `active_streams == 0` on the old `:6969` process (up to 8s drain) so in-flight
   `Blacksand Code` streams (`streamGenerateContent`, `/v1/chat/completions`) finish cleanly.
4. Requests the old `:6969` process to exit cleanly with code 0 via `/api/system/handover-exit`
   (or `/api/studio-bridge/handover-exit`) while spinning in a 5ms loop to bind the dual-stack
   `[::]:6969` socket (`IPV6_V6ONLY=0`, `SO_EXCLUSIVEADDRUSE=1`) and call `listen(2048)` the
   exact millisecond the old socket closes (`<15ms` kernel socket handover).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROUTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO_HANDOVER_LOG = os.environ.get(
    "BSL_HANDOVER_LOG",
    os.path.join(ROUTER_DIR, ".brain", "logs", "router-handover.log"),
)


def _log_handover(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        if os.path.exists(os.path.dirname(STUDIO_HANDOVER_LOG)):
            with open(STUDIO_HANDOVER_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def _get_active_streams(port: int) -> int:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/antifreeze/status", method="GET")
        with urllib.request.urlopen(req, timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return int(data.get("active_streams", 0) or 0)
    except Exception:
        return 0


def _get_listening_health_pid(port: int) -> int:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return int(data.get("pid", 0) or 0)
    except Exception:
        return 0


def _find_supervisor_root_pid(target_pid: int) -> int:
    """Walk up parent chain on Windows if parent is an app.watchdog or app.prewarm_handover supervisor."""
    if os.name != "nt" or target_pid <= 0:
        return target_pid
    try:
        ps_cmd = (
            f"$cur = {int(target_pid)}; "
            "for ($i = 0; $i -lt 6; $i++) { "
            "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId = $cur\" -ErrorAction SilentlyContinue; "
            "  if (-not $p -or -not $p.ParentProcessId) { break } "
            "  $parent = Get-CimInstance Win32_Process -Filter \"ProcessId = $($p.ParentProcessId)\" -ErrorAction SilentlyContinue; "
            "  if (-not $parent -or $parent.Name -ne 'python.exe') { break } "
            "  $cmd = [string]$parent.CommandLine; "
            "  if ($cmd -match 'app\\.dualstack_serve|app\\.watchdog|app\\.prewarm_handover') { $cur = [int]$parent.ProcessId } else { break } "
            "} "
            "Write-Output $cur"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        val = (res.stdout or "").strip()
        if val.isdigit():
            return int(val)
    except Exception:
        pass
    return target_pid


def _request_old_router_exit(port: int, old_pid: int) -> str:
    """Trigger the old router process to exit cleanly with code 0.
    Never calls /api/system/shutdown (which spawns a delayed bsl_kill.bat bomb),
    and verifies expected_pid so a stale handover worker never kills a newer router PID.
    """
    current_health_pid = _get_listening_health_pid(port)
    if old_pid > 0 and current_health_pid > 0 and current_health_pid != old_pid:
        return f"already_replaced_by_{current_health_pid}"

    pid_query = f"?expected_pid={int(old_pid)}" if old_pid > 0 else ""
    for endpoint in (
        f"/api/system/handover-exit{pid_query}",
        f"/api/studio-bridge/handover-exit{pid_query}",
    ):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}{endpoint}", method="POST")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if 200 <= resp.status < 300:
                    return endpoint.split("?", 1)[0]
        except urllib.error.HTTPError as he:
            if he.code == 409:
                return "pid_mismatch_409"
            if he.code in (401, 403, 404):
                continue
        except Exception:
            continue

    if old_pid > 0 and old_pid != os.getpid():
        # Verify old_pid is still the one listening before using taskkill
        recheck_pid = _get_listening_health_pid(port)
        if recheck_pid > 0 and recheck_pid != old_pid:
            return f"already_replaced_by_{recheck_pid}"
        try:
            root_pid = _find_supervisor_root_pid(old_pid)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(root_pid)],
                capture_output=True,
                check=False,
                timeout=2.5,
            )
            if root_pid != old_pid:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(old_pid)],
                    capture_output=True,
                    check=False,
                    timeout=2.0,
                )
            return "taskkill"
        except Exception:
            pass
    return "none"


def _bind_dualstack_fast(port: int, timeout_s: float = 8.0) -> tuple[socket.socket, float]:
    """Spin in a 5ms loop until `[::]:port` (IPv6+IPv4 dual-stack) binds and listens."""
    deadline = time.perf_counter() + timeout_s
    last_busy_ts = time.perf_counter()
    attempts = 0
    while True:
        attempts += 1
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        if os.name == "nt":
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("::", port))
            s.listen(2048)
            s.set_inheritable(True)
            unlistening_gap_ms = round((time.perf_counter() - last_busy_ts) * 1000.0, 2) if attempts > 1 else 0.0
            return s, unlistening_gap_ms
        except OSError:
            s.close()
            last_busy_ts = time.perf_counter()
            if last_busy_ts >= deadline:
                raise
            time.sleep(0.005)


def main() -> None:
    log_dir = os.path.join(ROUTER_DIR, ".brain", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_log_path = os.path.join(log_dir, "app.out.log")
    err_log_path = os.path.join(log_dir, "app.err.log")
    try:
        sys.stdout = open(out_log_path, "a", encoding="utf-8", buffering=1, errors="replace")
        sys.stderr = open(err_log_path, "a", encoding="utf-8", buffering=1, errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--old-pid", type=int, default=0)
    parser.add_argument("--port", type=int, default=6969)
    parser.add_argument("--state-file", type=str, default="")
    parser.add_argument("--drain-timeout", type=float, default=45.0)
    args = parser.parse_args()

    os.chdir(ROUTER_DIR)
    if ROUTER_DIR not in sys.path:
        sys.path.insert(0, ROUTER_DIR)

    # SUPERVISOR CONTINUITY GUARD:
    # When invoked without BSL_HANDOVER_SUPERVISED=1, this process acts as the
    # WMI-detached Supervisor Parent: it runs the pre-warmed handover child first,
    # and if that child ever exits with a non-zero code (rc != 0), it seamlessly
    # hands off to app.watchdog.run_supervised(port=args.port).
    if os.environ.get("BSL_HANDOVER_SUPERVISED") != "1":
        child_env = os.environ.copy()
        child_env["BSL_HANDOVER_SUPERVISED"] = "1"
        child_env["BSL_SUPERVISED"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUNBUFFERED"] = "1"
        child_cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--old-pid",
            str(args.old_pid),
            "--port",
            str(args.port),
            "--state-file",
            str(args.state_file or ""),
            "--drain-timeout",
            str(args.drain_timeout),
        ]
        _child_flags = 0x08000000 if os.name == "nt" else 0
        child_proc = subprocess.Popen(
            child_cmd,
            cwd=ROUTER_DIR,
            env=child_env,
            creationflags=_child_flags,
        )
        _log_handover(
            f"[BSL-PrewarmSupervisor] Supervisor pid={os.getpid()} watching pre-warmed child pid={child_proc.pid} on :{args.port}"
        )
        rc = child_proc.wait()
        if rc == 0:
            # Verify a successor is actually listening on :port before standing down
            successor_pid = 0
            for _ in range(12):
                successor_pid = _get_listening_health_pid(int(args.port or 6969))
                if successor_pid > 0 and successor_pid != child_proc.pid:
                    break
                time.sleep(0.20)
            if successor_pid > 0:
                _log_handover(
                    f"[BSL-PrewarmSupervisor] Child pid={child_proc.pid} exited cleanly (rc=0) and successor pid={successor_pid} is serving :{args.port}; supervisor standing down."
                )
                return
            _log_handover(
                f"[BSL-PrewarmSupervisor] Child pid={child_proc.pid} exited (rc=0) but :{args.port} has NO successor — engaging app.watchdog.run_supervised(port={args.port})!"
            )
            from app.watchdog import run_supervised
            run_supervised(port=int(args.port or 6969))
            return
        _log_handover(
            f"[BSL-PrewarmSupervisor] Child pid={child_proc.pid} exited unexpectedly (rc={rc}) — engaging app.watchdog.run_supervised(port={args.port})!"
        )
        from app.watchdog import run_supervised
        run_supervised(port=int(args.port or 6969))
        return

    os.environ["BSL_SUPERVISED"] = "1"

    t_start = time.perf_counter()
    import uvicorn
    import app.dualstack_serve as dualstack_serve
    import app.main as router_main

    router_main.load_config()
    cfg = router_main.cs_get_config()
    port = int(args.port or cfg.get("server", {}).get("port", 6969))

    config = uvicorn.Config(router_main.app, log_level="info")
    config.load()
    server = uvicorn.Server(config)
    dualstack_serve._install_accept_loop_guard()

    prewarm_ms = round((time.perf_counter() - t_start) * 1000.0, 1)

    # Wait up to drain_timeout (default 45.0s) for any in-flight IDE LLM stream
    # on the old process to finish completely before requesting socket handover.
    if args.old_pid > 0:
        drain_deadline = time.perf_counter() + max(1.0, float(args.drain_timeout or 45.0))
        zero_streak = 0
        while time.perf_counter() < drain_deadline:
            if _get_active_streams(port) == 0:
                zero_streak += 1
                if zero_streak >= 2:
                    break
                time.sleep(0.08)
            else:
                zero_streak = 0
                time.sleep(0.15)

    exit_method = _request_old_router_exit(port, args.old_pid)
    if exit_method.startswith("already_replaced_by_") or exit_method == "pid_mismatch_409":
        _log_handover(
            f"[BSL-PrewarmHandover] Aborting duplicate handover for old_pid={args.old_pid} ({exit_method}); standing down cleanly."
        )
        return

    sock, unlistening_gap_ms = _bind_dualstack_fast(port, timeout_s=8.0)

    lock_path = os.path.join(ROUTER_DIR, ".brain", "logs", f"router-{port}.lock")
    handover_lock_path = os.path.join(ROUTER_DIR, ".brain", "logs", f"handover-{port}.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w", encoding="ascii") as lfh:
            lfh.write(str(os.getpid()))
    except Exception:
        pass
    try:
        if os.path.exists(handover_lock_path):
            os.unlink(handover_lock_path)
    except Exception:
        pass

    if args.state_file:
        try:
            with open(args.state_file, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "new_pid": os.getpid(),
                        "old_pid": args.old_pid,
                        "exit_method": exit_method,
                        "prewarm_ms": prewarm_ms,
                        "unlistening_gap_ms": unlistening_gap_ms,
                        "swap_completed_at": time.time(),
                    },
                    fh,
                )
        except Exception:
            pass

    _log_handover(
        f"[BSL-PrewarmHandover] Pre-warmed in {prewarm_ms}ms (exit_method={exit_method}); "
        f"dual-stack [::]:{port} socket bound with unlistening_gap={unlistening_gap_ms}ms (pid={os.getpid()})"
    )

    threading.Thread(
        target=dualstack_serve._self_health_watchdog,
        args=(port,),
        daemon=True,
        name="dualstack-health-watchdog",
    ).start()

    try:
        server.run(sockets=[sock])
    finally:
        try:
            if os.path.exists(lock_path):
                os.unlink(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
