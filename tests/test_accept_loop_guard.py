"""Tests for the F8 accept-loop guard in app.dualstack_serve.

WHY THIS EXISTS
---------------
CPython 3.10's ProactorEventLoop closes the LISTENING socket whenever AcceptEx
completes with any OSError (Lib/asyncio/proactor_events.py:842-849) and never
re-arms. One aborted inbound connection therefore deafens the router
permanently: the process stays alive, health probes fail, the self-health
watchdog os._exit(3)s, and the supervisor respawns into the same trap until it
gives up -- a total :6969 outage. Reproduced deterministically 2026-08-29.

These tests drive the guard through a REAL ProactorEventLoop and a real
listening socket, then inject the failure by making the proactor's accept()
return a future that raises OSError(WinError 64). That is the exact shape
CPython delivers, and it keeps the tests fast and deterministic rather than
depending on RST timing.

Skipped wholesale on non-Windows, where ProactorEventLoop does not exist.
"""
from __future__ import annotations

import asyncio
import socket
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.dualstack_serve as ds  # noqa: E402

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="ProactorEventLoop / WinError 64 is Windows-only",
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _winerror64() -> OSError:
    """An OSError shaped exactly like the one AcceptEx delivers."""
    exc = OSError(22, "The specified network name is no longer available", None, 64, None)
    return exc


def _listening_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    return s


class _FailingAcceptProactor:
    """Wraps the REAL proactor, failing only the first N accept() calls.

    It must DELEGATE everything else: the ProactorEventLoop uses its proactor
    for its internal self-pipe (``recv``) and during ``close()``. A standalone
    double breaks both, producing AttributeError noise that has nothing to do
    with the behaviour under test.

    A pending future models "armed and waiting for a connection" -- what a
    healthy re-armed accept looks like.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, real, failures: int) -> None:
        self._loop = loop
        self._real = real
        self._remaining = failures
        self.accept_calls = 0

    def accept(self, sock):  # noqa: ANN001 - mirrors the asyncio API
        self.accept_calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            fut = self._loop.create_future()
            fut.set_exception(_winerror64())
            return fut
        # Armed and waiting: a future nobody completes.
        return self._loop.create_future()

    def __getattr__(self, name):  # delegate recv/close/etc to the real proactor
        return getattr(self._real, name)


def _install_on_fresh_loop(failures: int):
    """Install the guard, wire a fake proactor, and drive _start_serving once."""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    assert ds._install_accept_loop_guard() is True

    sock = _listening_socket()
    proactor = _FailingAcceptProactor(loop, loop._proactor, failures)
    loop._proactor = proactor  # type: ignore[attr-defined]

    def _proto():
        return asyncio.Protocol()

    loop._start_serving(_proto, sock)
    # Let the queued call_soon(loop) and its done-callbacks drain.
    loop.run_until_complete(asyncio.sleep(0.05))
    return loop, sock, proactor


# ── tests ─────────────────────────────────────────────────────────────────────

def test_guard_installs_and_is_idempotent():
    """Install returns True and re-installing does not double-wrap."""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        assert ds._install_accept_loop_guard() is True
        from asyncio.proactor_events import BaseProactorEventLoop

        first = BaseProactorEventLoop._start_serving
        assert getattr(first, "_bsl_accept_guard", False) is True

        assert ds._install_accept_loop_guard() is True
        assert BaseProactorEventLoop._start_serving is first, (
            "re-install must not wrap the already-wrapped function again"
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_oserror_does_not_close_listening_socket():
    """The core guarantee: WinError 64 must NOT close the listener."""
    loop, sock, proactor = _install_on_fresh_loop(failures=1)
    try:
        assert sock.fileno() != -1, (
            "listening socket was closed -- the guard failed to re-arm and the "
            "router would now be permanently deaf"
        )
        assert proactor.accept_calls >= 2, (
            "expected an initial arm plus at least one re-arm after the OSError"
        )
    finally:
        sock.close()
        loop.close()
        asyncio.set_event_loop(None)


def test_rearmed_future_gets_a_done_callback():
    """A re-armed future must be registered, else the loop dies silently.

    Regression guard: the first implementation re-armed inside an `except`
    block and relied on the `else:` clause to attach the done-callback. `else`
    is skipped when an exception was handled, so the future was orphaned and
    the accept loop stopped just as silently as the original bug -- while
    logging that it had succeeded.
    """
    loop, sock, proactor = _install_on_fresh_loop(failures=1)
    try:
        fut = loop._accept_futures.get(sock.fileno())
        assert fut is not None, "re-armed future was never registered"
        assert fut._callbacks, "re-armed future has no done-callback: loop is orphaned"
    finally:
        sock.close()
        loop.close()
        asyncio.set_event_loop(None)


def test_heartbeat_is_published_for_the_watchdog():
    """The watchdog reads this heartbeat; without it the F8-B grace period dies.

    The watchdog runs in a daemon thread where asyncio.get_event_loop() raises
    RuntimeError, so it cannot inspect the loop. The heartbeat is the only
    cross-thread signal that the guard is actively keeping the listener alive.
    """
    ds._accept_guard_last_rearm = 0.0
    loop, sock, _ = _install_on_fresh_loop(failures=1)
    try:
        assert ds._accept_guard_last_rearm > 0.0, (
            "guard did not publish a heartbeat; the watchdog cannot distinguish "
            "a recoverable accept fault from a wedged loop"
        )
    finally:
        sock.close()
        loop.close()
        asyncio.set_event_loop(None)


def test_burst_exhaustion_keeps_serving():
    """Exceeding the burst budget must NOT close the listener.

    Regression guard: the first implementation called sock.close() on burst
    exhaustion, which is precisely the failure mode this guard exists to
    prevent. It reproduced the outage exactly (verified 2026-08-29 17:13).
    Exhaustion now only sets a flag; the watchdog owns the exit decision.
    """
    failures = ds._ACCEPT_GUARD_BURST + 25
    ds._accept_guard_exhausted = False
    loop, sock, proactor = _install_on_fresh_loop(failures=failures)
    try:
        assert sock.fileno() != -1, (
            "listener was closed on burst exhaustion -- this is the original bug"
        )
        assert ds._accept_guard_exhausted is True, (
            "exhaustion must be flagged so the watchdog can act on it"
        )
        assert proactor.accept_calls > ds._ACCEPT_GUARD_BURST, (
            "guard stopped re-arming instead of continuing to serve"
        )
    finally:
        sock.close()
        loop.close()
        asyncio.set_event_loop(None)


def test_closed_socket_falls_back_to_original_behaviour():
    """If the socket is genuinely gone, do not spin -- let the original path run."""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        assert ds._install_accept_loop_guard() is True
        sock = _listening_socket()
        proactor = _FailingAcceptProactor(loop, loop._proactor, failures=1)
        loop._proactor = proactor  # type: ignore[attr-defined]

        # Close the socket BEFORE the failing completion is processed, so
        # sock.fileno() == -1 when the guard inspects it.
        loop._start_serving(lambda: asyncio.Protocol(), sock)
        sock.close()
        loop.run_until_complete(asyncio.sleep(0.05))

        # Must not raise, must not spin re-arming a dead socket.
        assert sock.fileno() == -1
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_guard_noop_on_selector_loop():
    """On a SelectorEventLoop the guard must decline to install."""
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        assert ds._install_accept_loop_guard() is False
    finally:
        loop.close()
        asyncio.set_event_loop(None)
