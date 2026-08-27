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
import os
import socket
import sys
import time


def make_dualstack_socket(port: int) -> socket.socket:
    """Create a dual-stack (IPv4+IPv6) listening socket on ``[::]:port``."""
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
    s.bind(("::", port))
    s.listen(2048)
    s.set_inheritable(True)
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description="BSL Router dual-stack server")
    parser.add_argument("--port", type=int, default=6969)
    parser.add_argument("--log-level", default="info")
    args, _unknown = parser.parse_known_args()

    import uvicorn

    config = uvicorn.Config("app.main:app", log_level=args.log_level)
    server = uvicorn.Server(config)
    sock = make_dualstack_socket(args.port)
    print(
        f"[DualStack] listening on [::]:{args.port} (IPv6+IPv4, V6ONLY=0)",
        flush=True,
    )

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


if __name__ == "__main__":
    sys.exit(main())
