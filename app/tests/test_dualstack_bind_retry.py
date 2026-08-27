"""Bind-retry tests for make_dualstack_socket (server-stop bug fix 2026-08-27).

Proves:
  - bind retries while the port is held (zombie holder scenario) and
    succeeds once the holder releases it
  - bind gives up with OSError after retry_total_s while port stays held

Uses EPHEMERAL ports only — NEVER :6969 or the live server.
"""

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.dualstack_serve import make_dualstack_socket


def _block_ephemeral_port() -> tuple[socket.socket, int]:
    """Bind an ephemeral IPv6 port and return (blocker_socket, port)."""
    blocker = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    blocker.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    blocker.bind(("::", 0))
    port = blocker.getsockname()[1]
    assert port != 6969
    return blocker, port


def test_bind_retry_succeeds_after_release():
    blocker, port = _block_ephemeral_port()
    result: dict = {}

    def worker():
        try:
            result["sock"] = make_dualstack_socket(port, retry_total_s=6.0)
        except Exception as e:  # pragma: no cover - surfaced via assert below
            result["err"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(1.0)  # let the first bind attempt hit the busy port
    blocker.close()  # release the zombie-held port
    t.join(timeout=20.0)

    assert not t.is_alive(), "bind-retry thread hung past join timeout"
    assert "err" not in result, f"unexpected bind error: {result.get('err')!r}"
    sock = result["sock"]
    assert sock is not None
    try:
        assert sock.getsockname()[1] == port
        # The socket must actually serve the port: a client connect succeeds.
        client = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        client.settimeout(5.0)
        try:
            client.connect(("::1", port))
        finally:
            client.close()
    finally:
        sock.close()


def test_bind_retry_gives_up_while_held():
    blocker, port = _block_ephemeral_port()
    try:
        t0 = time.monotonic()
        try:
            make_dualstack_socket(port, retry_total_s=2.0)
            raise AssertionError("expected OSError while port is held")
        except OSError:
            pass
        elapsed = time.monotonic() - t0
        # Retry budget is 2s (step 2s) — must give up promptly, not hang.
        assert elapsed < 15.0, f"give-up took too long: {elapsed:.1f}s"
    finally:
        blocker.close()
