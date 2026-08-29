"""Dual-stack entrypoint for BSL Router (KEY FIX 2026-08-27).

WHY: uvicorn ``--host 0.0.0.0`` binds IPv4-only, while Windows resolves
``localhost`` to ``::1`` first. IPv6-first clients (Node fetch, some Python
stacks) then get an instant ECONNREFUSED that looks like a "transient router
restart" â€” it is not; the probe never reaches the server. ``--host ::`` alone
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
import asyncio
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

# Single-instance lock (KEY FIX 2026-08-28, restart fork-bomb regression):
# POST /api/version/restart spawns a fresh dualstack_serve without the
# BSL_SUPERVISED flag, so the spawn re-enters the supervision gate and
# becomes a SECOND supervisor while the old lineage is still draining â€”
# two lineages then fight over one exclusive port for minutes (live
# incident 18:19-18:21: 15 spawns from 13 different parents, WinError 64
# accept-loop deaths, kill/respawn ping-pong). Every serving process must
# now win an exclusive lock file before binding.
_LOCK_RETRY_STEP_S = 2.0

# Accept-loop guard (KEY FIX 2026-08-29, WinError 64 deaf-router).
# CPython's ProactorEventLoop closes the LISTENING socket on ANY OSError
# from AcceptEx and never re-arms the accept, permanently deafening the
# router. The guard rewrites the inner accept callback to re-arm on the
# SAME socket instead. See _install_accept_loop_guard().
_ACCEPT_GUARD_BURST = 50          # max re-arms per socket within the window
_ACCEPT_GUARD_WINDOW_S = 1.0      # rolling window for burst counting
# Chosen 50 re-arms per 1.0s: far above normal traffic noise, far below a tight
# spin. Exceeding it means the listener is genuinely wedged, not merely hit by an
# aborted connection, so the guard stands down and lets the watchdog recover.
_accept_guard_exhausted = False   # set True when the guard stands down (F8-B)
_ACCEPT_GUARD_LOG_EVERY = 500     # audit every Nth resurrection after the first 3
# Monotonic timestamp of the guard's most recent successful re-arm. The
# self-health watchdog runs in a DAEMON THREAD, where
# asyncio.get_event_loop() raises RuntimeError ("There is no current event
# loop in thread ...") — so the watchdog cannot inspect the loop's
# _accept_futures to learn whether the listener is still bound. This
# heartbeat is the cross-thread-safe substitute: a plain float assignment is
# atomic under the GIL, needs no lock, and tells the watchdog whether the
# guard is actively keeping the listener alive right now.
_accept_guard_last_rearm = 0.0


def _audit_line(line: str) -> None:
    """Append-only forensic line to .brain/logs/restart_audit.log."""
    try:
        _audit = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".brain", "logs", "restart_audit.log",
        )
        os.makedirs(os.path.dirname(_audit), exist_ok=True)
        with open(_audit, "a", encoding="utf-8") as _fh:
            _fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """True if `pid` is a live process (Windows-safe, access-denied = alive)."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            # ERROR_ACCESS_DENIED means an elevated process IS alive.
            return kernel32.GetLastError() == 5
        try:
            WAIT_TIMEOUT = 0x102
            return kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False


def _probe_health(port: int) -> bool:
    """Cheap /health probe against an existing router on this port."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _probe_health_dual(port: int) -> bool:
    """True if EITHER stack (IPv4 127.0.0.1 or IPv6 [::1]) answers /health.

    KEY FIX 2026-08-29 (split-brain socket, live incident 04:20): after a
    Windows network-stack hiccup (sleep/resume, interface bounce) the IPv6
    accept path on the dual-stack listening socket can die while the
    IPv4-mapped path keeps serving fine â€” the [::1]-only self-probe failed
    3/3 while real ::ffff:127.0.0.1 requests streamed 200 OK, so the
    watchdog killed a HEALTHY router. A router is dead only when BOTH
    stacks are unreachable for the full failure window.
    """
    for host in ("127.0.0.1", "[::1]"):
        try:
            req = urllib.request.Request(
                f"http://{host}:{port}/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception:
            continue
    return False


def acquire_instance_lock(port: int, intended_successor: bool = False,
                          max_wait_s: float = 150.0) -> bool:
    """Win the single-instance lock for serving ``:port``.

    Returns True when THIS process owns the lock and may bind.
    Returns False when a healthy owner already exists and we must stand
    down (exit 0) â€” the duplicate dissolves instead of fighting the port.

    Rules per holder state:
      - holder PID dead                      -> break stale lock, take over
      - holder alive + healthy (/health 200):
          intended successor (restart flow)  -> WAIT out the holder's drain
            window (GRACEFUL_RESTART_MAX_WAIT_S=120s + slack); the old
            holder exits after draining, then we take over
          anyone else (duplicate spawn)      -> stand down immediately
      - holder alive + NOT healthy (draining/zombie) -> wait for death up
        to max_wait_s; at deadline give up (exit nonzero upstream)
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lock_path = os.path.join(root, ".brain", "logs", f"router-{port}.lock")
    deadline = time.monotonic() + max_wait_s
    waited = 0.0
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            if intended_successor:
                # Once we own the lock we are THE router â€” future respawns of
                # this lineage must behave as normal duplicates again.
                os.environ.pop("BSL_INTENDED_SUCCESSOR", None)
            _audit_line(
                f"LOCK_ACQUIRED pid={os.getpid()} port={port} "
                f"successor={intended_successor} waited={waited:.0f}s"
            )
            return True
        except FileExistsError:
            holder_pid = 0
            try:
                with open(lock_path, "r", encoding="ascii") as fh:
                    holder_pid = int((fh.read() or "0").strip() or "0")
            except (OSError, ValueError):
                holder_pid = 0
            if holder_pid == os.getpid():
                return True  # already ours (re-entry)
            if not _pid_alive(holder_pid):
                try:
                    os.unlink(lock_path)
                    _audit_line(f"LOCK_STALE_BROKEN pid={os.getpid()} dead_holder={holder_pid}")
                except OSError:
                    pass
                continue
            if _probe_health(port):
                if not intended_successor:
                    _audit_line(
                        f"LOCK_STANDOWN pid={os.getpid()} healthy_holder={holder_pid}"
                    )
                    return False
                # Successor: holder is draining (healthy now, exiting soon).
            else:
                # Holder alive but sick: zombie/dying â€” wait it out.
                pass
            if time.monotonic() >= deadline:
                _audit_line(
                    f"LOCK_TIMEOUT pid={os.getpid()} holder={holder_pid} "
                    f"successor={intended_successor}"
                )
                return False
            time.sleep(_LOCK_RETRY_STEP_S)
            waited += _LOCK_RETRY_STEP_S


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
            # Very old kernels â€” degrade to whatever the OS default is.
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
                f"[DualStack] bind busy on [::]:{port} ({e!r}) â€” "
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

    ``os._exit`` bypasses finally/atexit â€” intentional, the event loop may
    be wedged while the socket is still held, and we need the OS to reap
    the port so the respawn (with bind-retry) can recover it.

    F8-B: with the accept-loop guard (F8-A) in place, a single WinError 64
    is now recoverable in-process, so the watchdog gives the guard a chance
    before exiting. It reads the guard's monotonic re-arm heartbeat rather
    than inspecting the event loop, because this function runs in a daemon
    thread where ``asyncio.get_event_loop()`` raises ``RuntimeError``. The
    exit reasons are logged distinctly so the paths stay separable.
    """
    failures = 0
    while True:
        time.sleep(interval_s)
        ok = _probe_health_dual(port)
        if ok:
            failures = 0
            continue
        failures += 1
        print(
            f"[DualStack] health watchdog: probe failed ({failures}/{max_failures})",
            flush=True,
        )
        if failures >= max_failures:
            # F8-B: distinguish the two last-resort paths.
            #   - guard_active: the guard re-armed the listener very recently,
            #     so the fault is the recoverable WinError-64 class. Give it one
            #     more interval before pulling the plug.
            #   - no_guard_activity: nothing is re-arming (guard never installed,
            #     or the loop is genuinely wedged). Exit promptly.
            #
            # We deliberately do NOT introspect the event loop here. This runs in
            # a daemon thread where asyncio.get_event_loop() raises RuntimeError,
            # which an `except Exception: pass` would silently swallow — leaving
            # the grace period permanently unreachable. Read the guard's
            # monotonic heartbeat instead.
            since_rearm = time.monotonic() - _accept_guard_last_rearm
            guard_active = (
                _accept_guard_last_rearm > 0.0 and since_rearm < interval_s * 2
            )

            if guard_active:
                print(
                    f"[DualStack] watchdog: guard re-armed {since_rearm:.1f}s ago "
                    f"— recoverable fault, waiting one more interval",
                    flush=True,
                )
                time.sleep(interval_s)
                if _probe_health_dual(port):
                    failures = 0
                    print(
                        "[DualStack] watchdog: health recovered via guard — "
                        "standing down",
                        flush=True,
                    )
                    _audit_line(
                        f"WATCHDOG_STOOD_DOWN pid={os.getpid()} "
                        f"port={port} reason=guard_recovered"
                    )
                    continue
                reason = "guard_active_but_unhealthy"
            else:
                reason = "no_guard_activity"

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
                        f"reason={reason} port={port}\n"
                    )
            except Exception:
                pass
            print(
                f"[DualStack] watchdog: {max_failures} consecutive health failures "
                f"({reason}) â€” exiting",
                flush=True,
            )
            os._exit(3)


def _install_accept_loop_guard() -> bool:
    """Patch ``BaseProactorEventLoop._start_serving`` so AcceptEx OSError
    re-arms the LISTENING socket instead of closing it.

    WHY (2026-08-29): CPython 3.10.11 ``proactor_events.py:842-849`` catches
    ``OSError`` from ``AcceptEx``, calls ``sock.close()`` on the listening
    socket, and never re-arms ``self._proactor.accept(sock)``. One aborted
    inbound connection permanently deafens the router; the self-health
    watchdog then ``os._exit(3)``s and the supervisor respawns into the same
    trap until it gives up â€” total ``:6969`` outage.

    The patch wraps the inner ``loop`` callback of ``_start_serving``. When
    ``f.result()`` raises ``OSError`` and the socket is still valid, the
    guard re-arms ``self._proactor.accept(sock)`` on the SAME socket,
    keeping the listening socket alive with no rebind window.

    Safeguards:
      - No-op on non-Windows / non-Proactor loops (loop type guard).
      - Idempotent (re-install is a safe no-op).
      - If ``sock.fileno() == -1`` the socket is genuinely gone â€” fall back
        to the original ``sock.close()`` behaviour rather than spinning.
      - Burst guard: ``_ACCEPT_GUARD_BURST`` re-arms within
        ``_ACCEPT_GUARD_WINDOW_S`` per socket. If exceeded, the guard sets
        ``_accept_guard_exhausted = True`` and falls through to the original
        path so the supervisor can recover (F8-B).

    Returns True when the guard was installed, False on a non-Proactor loop.
    """
    global _accept_guard_exhausted

    # No-op on non-Windows / non-Proactor loops.
    # NOTE: BaseProactorEventLoop is NOT exported on the `asyncio` package â€”
    # it lives in asyncio.proactor_events. Referencing asyncio.BaseProactorEventLoop
    # raises AttributeError and crashed every child on boot (rc=1, 2026-08-29 15:23).
    try:
        from asyncio.proactor_events import BaseProactorEventLoop
    except ImportError:
        return False

    loop = asyncio.get_event_loop_policy().get_event_loop()
    if not isinstance(loop, BaseProactorEventLoop):
        return False

    # Idempotent: the wrapper already carries the marker.
    orig = BaseProactorEventLoop._start_serving
    if getattr(orig, "_bsl_accept_guard", False):
        return True

    # Capture module logger lazily (None if unavailable).
    try:
        from asyncio.log import logger as _alogger
        _logger_debug = _alogger.debug
    except Exception:
        _logger_debug = None

    def _guard_start_serving(self, protocol_factory, sock,
                             sslcontext=None, server=None, backlog=100,
                             ssl_handshake_timeout=None):
        # Per-socket burst counter: {fileno: [timestamps]}.
        burst = {}
        # Lifetime resurrection count per socket, used to throttle audit logging
        # (the burst window resets, so it cannot serve as a stable log key).
        total = {}
        # Listening port for audit lines (best-effort; 0 if unknown).
        try:
            _port = sock.getsockname()[1]
        except Exception:
            _port = 0

        def loop(f=None):
            try:
                if f is not None:
                    conn, addr = f.result()
                    if self._debug:
                        _logger_debug and _logger_debug(
                            "%r got a new connection from %r: %r",
                            server, addr, conn,
                        )
                    protocol = protocol_factory()
                    if sslcontext is not None:
                        self._make_ssl_transport(
                            conn, protocol, sslcontext, server_side=True,
                            extra={'peername': addr}, server=server,
                            ssl_handshake_timeout=ssl_handshake_timeout,
                        )
                    else:
                        self._make_socket_transport(
                            conn, protocol,
                            extra={'peername': addr}, server=server,
                        )
                if self.is_closed():
                    return
                f = self._proactor.accept(sock)
            except OSError as exc:
                if sock.fileno() == -1:
                    # Socket genuinely gone â€” original behaviour.
                    if self._debug:
                        _logger_debug and _logger_debug(
                            "Accept failed on socket %r", sock, exc_info=True
                        )
                    sock.close()
                    return
                # F8-A: re-arm the listening socket instead of closing it.
                now = time.monotonic()
                key = sock.fileno()
                stamps = burst.get(key, [])
                stamps = [t for t in stamps if now - t < _ACCEPT_GUARD_WINDOW_S]
                stamps.append(now)
                burst[key] = stamps
                count = len(stamps)
                total[key] = total.get(key, 0) + 1
                if count > _ACCEPT_GUARD_BURST:
                    # F8-B: budget exhausted. Do NOT close the listening socket â€”
                    # closing it IS the failure mode we exist to prevent, and doing
                    # so here reproduced the original outage exactly (verified
                    # 2026-08-29 17:13: count reached 50, guard closed the socket,
                    # router went deaf, health FAIL with the process still alive).
                    #
                    # Instead: flag exhaustion so the self-health watchdog owns the
                    # decision to exit, and keep re-arming. A hot spin is bounded by
                    # the fact that each re-arm requires a real failed AcceptEx
                    # completion â€” we are not looping without work.
                    global _accept_guard_exhausted
                    if not _accept_guard_exhausted:
                        _accept_guard_exhausted = True
                        _audit_line(
                            f"ACCEPT_GUARD_EXHAUSTED pid={os.getpid()} port={_port} "
                            f"count={count} window_s={_ACCEPT_GUARD_WINDOW_S} "
                            f"action=keep_serving_watchdog_decides"
                        )
                        print(
                            f"[DualStack] accept-loop guard: burst budget exhausted "
                            f"({count} re-arms in {_ACCEPT_GUARD_WINDOW_S}s) â€” still "
                            f"serving, watchdog will decide",
                            flush=True,
                        )
                    # Reset the window so the counter reflects the CURRENT burst
                    # rather than staying permanently over budget.
                    burst[key] = [now]
                # Resurrect: re-arm accept on the SAME listening socket.
                try:
                    f = self._proactor.accept(sock)
                except Exception:
                    # Re-arm itself failed â€” fall back to original.
                    if self._debug:
                        _logger_debug and _logger_debug(
                            "Accept re-arm failed on socket %r", sock,
                            exc_info=True,
                        )
                    sock.close()
                    return
                # Publish the heartbeat on EVERY successful re-arm, not just the
                # logged ones — the watchdog thread reads this to tell a
                # recoverable accept fault from a genuinely wedged loop.
                global _accept_guard_last_rearm
                _accept_guard_last_rearm = now
                # F8-A2: make resurrections visible WITHOUT flooding the shared
                # audit log. An unthrottled line-per-resurrection wrote ~900
                # lines/second during a real burst (measured 2026-08-29 17:16:
                # 18.5k lines / 1.55 MB from two probe runs), which would bury
                # every genuine SPAWN/WATCHDOG_EXIT record operators rely on.
                # Log the first few, then every _LOG_EVERY-th, with a running
                # total so the true volume is never understated.
                _n = total[key]
                if _n <= 3 or _n % _ACCEPT_GUARD_LOG_EVERY == 0:
                    _audit_line(
                        f"ACCEPT_LOOP_RESURRECTED pid={os.getpid()} port={_port} "
                        f"winerror={getattr(exc, 'winerror', None)} "
                        f"burst={count} total={_n}"
                    )
                    print(
                        f"[DualStack] accept-loop guard: resurrected listener on "
                        f"[::]:{_port} after OSError "
                        f"winerror={getattr(exc, 'winerror', None)} "
                        f"(burst={count}/{_ACCEPT_GUARD_BURST} total={_n})",
                        flush=True,
                    )
                # CRITICAL: register the re-armed future HERE. The `else:` clause
                # below only runs when NO exception occurred, so a future re-armed
                # inside this handler would otherwise never get its done-callback
                # attached â€” the accept loop would stop just as silently as the
                # bug we are fixing, only now with a log line claiming success.
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)
                return
            except asyncio.CancelledError:
                sock.close()
                return
            else:
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)

        self.call_soon(loop)

    # Carry the marker and the original reference.
    _guard_start_serving._bsl_accept_guard = True  # type: ignore[attr-defined]
    _guard_start_serving._bsl_original = orig  # type: ignore[attr-defined]

    BaseProactorEventLoop._start_serving = _guard_start_serving
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="BSL Router dual-stack server")
    parser.add_argument("--port", type=int, default=6969)
    parser.add_argument("--log-level", default="info")
    args, _unknown = parser.parse_known_args()

    # B1 SUPERVISION GATE (2026-08-27): the UI auto_restart toggle was dead
    # code â€” its gate lived in app/main.py's __main__, a path production
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
                # SINGLE-INSTANCE PRE-GATE (2026-08-28 fork-bomb fix): never
                # create a SECOND supervision lineage while a healthy router
                # already answers /health â€” a duplicate supervisor is exactly
                # what made restarts fight over the port. Intended successors
                # (restart flow) skip this gate: the old holder is still
                # healthy during its drain window.
                if (os.environ.get("BSL_INTENDED_SUCCESSOR") != "1"
                        and _probe_health(args.port)):
                    _audit_line(
                        f"GATE_STANDOWN pid={os.getpid()} "
                        f"reason=healthy_router_already_running"
                    )
                    print(
                        "[DualStack] healthy router already serving â€” standing down "
                        "(no duplicate supervision)",
                        flush=True,
                    )
                    return
                from app.watchdog import run_supervised
                run_supervised(port=args.port)
                return
        except FileNotFoundError:
            pass  # no config.yaml â€” serve unsupervised
        except Exception as _e:
            # Never let a console-encoding error (cp1252 vs non-ASCII, live
            # incident 2026-08-27) crash the process â€” ASCII-only message.
            try:
                print(f"[DualStack] supervision gate failed ({type(_e).__name__}) - serving unsupervised", flush=True)
            except Exception:
                pass

    import uvicorn

    config = uvicorn.Config("app.main:app", log_level=args.log_level)
    server = uvicorn.Server(config)

    # SINGLE-INSTANCE LOCK (2026-08-28 fork-bomb fix): exactly ONE serving
    # process may bind this port. Duplicates stand down before touching the
    # socket; intended successors wait out the old holder's drain window.
    _intended = os.environ.get("BSL_INTENDED_SUCCESSOR") == "1"
    if not acquire_instance_lock(args.port, intended_successor=_intended):
        print(
            "[DualStack] another instance owns this port â€” standing down",
            flush=True,
        )
        return  # exit 0: a watchdog parent reads this as "stand down" too

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
                f"parent_cmd={(_pcmd or '(empty â€” parent already exited)')[:180]}\n"
            )
    except Exception as _audit_exc:
        print(f"[DualStack] spawn audit failed: {_audit_exc!r}", flush=True)

    # ACCEPT-LOOP GUARD (KEY FIX 2026-08-29, WinError 64 deaf-router):
    # rewrite the ProactorEventLoop accept callback so an OSError from
    # AcceptEx re-arms the listening socket instead of closing it. No-op
    # on non-Windows / non-Proactor loops. Must run before server.run so
    # the patch is in place before the first accept is armed.
    _guard_installed = _install_accept_loop_guard()
    if _guard_installed:
        print(
            f"[DualStack] accept-loop guard installed (burst={_ACCEPT_GUARD_BURST} "
            f"per {_ACCEPT_GUARD_WINDOW_S}s)",
            flush=True,
        )

    try:
        server.run(sockets=[sock])
    finally:
        # Release the single-instance lock so an immediate respawn wins it
        # without stale-break heuristics.
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.unlink(os.path.join(_root, ".brain", "logs", f"router-{args.port}.lock"))
        except OSError:
            pass
    print(
        "[DualStack] server.run returned â€” exiting so supervisor can restart cleanly",
        flush=True,
    )


if __name__ == "__main__":
    sys.exit(main())
