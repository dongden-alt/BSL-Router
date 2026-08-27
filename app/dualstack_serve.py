"""Dual-stack entrypoint for BSL Router (KEY FIX 2026-08-27).

WHY: uvicorn ``--host 0.0.0.0`` binds IPv4-only, while Windows resolves
``localhost`` to ``::1`` first. IPv6-first clients (Node fetch, some Python
stacks) then get an instant ECONNREFUSED that looks like a "transient router
restart" — it is not; the probe never reaches the server. ``--host ::`` alone
is ALSO wrong on Windows (default IPV6_V6ONLY=1 makes it IPv6-only, killing
every 127.0.0.1 client, verified on staging 2026-08-27).

The fix is ONE listening socket bound to ``::`` with ``IPV6_V6ONLY=0``, which
serves BOTH families on all platforms (Linux default is already 0; the
explicit setsockopt is harmless there).

Usage (replaces ``python -m uvicorn app.main:app --host ...``):
    python -m app.dualstack_serve --port 6969

All three spawn sites route through here:
  - scripts/bslrouter.ps1 (background + window launch)
  - POST /api/version/restart relaunch (app/main.py)
"""
from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
import threading
import time
import urllib.request

# Bind-retry window (KEY FIX 2026-08-27, server-stop bug):
# a zombie router process can linger holding :6969 with
# SO_EXCLUSIVEADDRUSE after its accept loop died (WinError 64). A fresh
# spawn then gets WSAEACCES 10013 on bind and dies instantly, stacking
# failed spawns. Retry until the zombie is reaped / port is released.
_BIND_RETRY_TOTAL_S = 120.0
_BIND_RETRY_STEP_S = 2.0


def make_dualstack_socket(port: int, retry_total_s: float = _BIND_RETRY_TOTAL_S) -> socket.socket:
    """Create a dual-stack (IPv4+IPv6) listening socket on ``[::]:port``.

    Retries up to ``retry_total_s`` seconds while the port is held by a
    zombie previous instance (WSAEACCES 10013 / WSAEADDRINUSE 10048 on
    Windows; EACCES / EADDRINUSE elsewhere). Other errors raise immediately.
    """
    deadline = time.monotonic() + retry_total_s
    while True:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            # Core of the fix: allow the IPv6 socket to accept IPv4-mapped clients.
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            # Very old kernels — degrade to whatever the OS default is.
            pass
        if os.name == "nt":
            # Windows: SO_REUSEADDR allows port hijacking by another process;
            # EXCLUSIVE is the safe inverse and is what uvicorn itself uses there.
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("::", port))
        except OSError as e:
            s.close()
            busy = False
            if os.name == "nt":
                busy = getattr(e, "winerror", None) in (10013, 10048)
            else:
                busy = getattr(e, "errno", None) in (errno.EACCES, errno.EADDRINUSE)
            if not busy:
                raise
            if time.monotonic() >= deadline:
                raise
            print(
                f"[DualStack] bind busy on [::]:{port} ({e!r}) — "
                f"retrying up to {retry_total_s:.0f}s total (zombie holder?)",
                flush=True,
            )
            time.sleep(_BIND_RETRY_STEP_S)
            continue
        s.listen(2048)
        s.set_inheritable(True)
        return s


def _self_health_watchdog(port: int, interval_s: float = 10.0, max_failures: int = 3) -> None:
    """Daemon thread: probe ``/health``; ``os._exit(3)`` after N consecutive misses.

    ``os._exit`` bypasses finally/atexit — intentional, the event loop may
    be wedged while the socket is still held, and we need the OS to reap
    the port so the respawn (with bind-retry) can recover it.
    """
    failures = 0
    while True:
        time.sleep(interval_s)
        try:
            req = urllib.request.Request(
                f"http://[::1]:{port}/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = 200 <= resp.status < 300
        except Exception:
            ok = False
        if ok:
            failures = 0
            continue
        failures += 1
        print(
            f"[DualStack] health watchdog: probe failed ({failures}/{max_failures})",
            flush=True,
        )
        if failures >= max_failures:
            _pid = os.getpid()
            try:
                _audit = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    ".brain", "logs", "restart_audit.log",
                )
                os.makedirs(os.path.dirname(_audit), exist_ok=True)
                with open(_audit, "a", encoding="utf-8") as _fh:
                    _fh.write(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} WATCHDOG_EXIT pid={_pid} "
                        f"reason=health_probe_failed\n"
                    )
            except Exception:
                pass
            print(
                f"[DualStack] watchdog: {max_failures} consecutive health failures — exiting",
                flush=True,
            )
            os._exit(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="BSL Router dual-stack server")
    parser.add_argument("--port", type=int, default=6969)
    parser.add_argument("--log-level", default="info")
    args, _unknown = parser.parse_known_args()

    # B1 SUPERVISION GATE (2026-08-27): the UI auto_restart toggle was dead
    # code — its gate lived in app/main.py's __main__, a path production
    # never takes (bslrouter.ps1 and /api/version/restart both spawn THIS
    # module). Honor config.watchdog.auto_restart here instead. The env guard
    # prevents fork-bombing: a supervised child must serve, never re-enter
    # supervision. Config is read raw (yaml only, no app.main import) so the
    # parent stays tiny and independent.
    if os.environ.get("BSL_SUPERVISED") != "1":
        try:
            import yaml
            _cfg_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.yaml",
            )
            with open(_cfg_path, "r", encoding="utf-8") as _fh:
                _cfg = yaml.safe_load(_fh) or {}
            if (_cfg.get("watchdog") or {}).get("auto_restart") is True:
                from app.watchdog import run_supervised
                run_supervised(port=args.port)
                return
        except FileNotFoundError:
            pass  # no config.yaml — serve unsupervised
        except Exception as _e:
            # Never let a console-encoding error (cp1252 vs non-ASCII, live
            # incident 2026-08-27) crash the process — ASCII-only message.
            try:
                print(f"[DualStack] supervision gate failed ({type(_e).__name__}) - serving unsupervised", flush=True)
            except Exception:
                pass

    import uvicorn

    config = uvicorn.Config("app.main:app", log_level=args.log_level)
    server = uvicorn.Server(config)
    sock = make_dualstack_socket(args.port)
    print(
        f"[DualStack] listening on [::]:{args.port} (IPv6+IPv4, V6ONLY=0)",
        flush=True,
    )

    # SELF-HEALTH WATCHDOG (KEY FIX 2026-08-27, server-stop bug):
    # if the accept loop dies (WinError 64) the process lingers holding
    # :6969 while serving nothing. Probe /health; after 3 consecutive
    # failures hard-exit so the supervisor's respawn (now with bind-retry)
    # recovers the port.
    threading.Thread(
        target=_self_health_watchdog,
        args=(args.port,),
        daemon=True,
        name="dualstack-health-watchdog",
    ).start()

    # SPAWN-SIDE AUDIT (2026-08-27): mystery spawns at 15:25/15:44/16:09/16:11/
    # 16:19 went through NO logged endpoint. Record who our parent is at every
    # start so the next unexplained spawn is attributable from this file alone.
    try:
        import subprocess as _sp
        _audit = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".brain", "logs", "restart_audit.log")
        os.makedirs(os.path.dirname(_audit), exist_ok=True)
        _ppid = os.getppid()
        try:
            _pcmd = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={_ppid}').CommandLine"],
                capture_output=True, text=True, timeout=8,
            ).stdout.strip()
        except Exception:
            _pcmd = "(lookup failed)"
        with open(_audit, "a", encoding="utf-8") as _fh:
            _fh.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} SPAWN pid={os.getpid()} ppid={_ppid} "
                f"parent_cmd={(_pcmd or '(empty — parent already exited)')[:180]}\n"
            )
    except Exception as _audit_exc:
        print(f"[DualStack] spawn audit failed: {_audit_exc!r}", flush=True)

    server.run(sockets=[sock])
    print(
        "[DualStack] server.run returned — exiting so supervisor can restart cleanly",
        flush=True,
    )


if __name__ == "__main__":
    sys.exit(main())
