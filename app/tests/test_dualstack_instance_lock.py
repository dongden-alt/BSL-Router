"""Tests for the single-instance lock (2026-08-28 restart fork-bomb fix).

Covers:
  1. basic acquisition + lock file contains our PID
  2. re-entry (holder == self) returns True
  3. stale lock (dead holder PID) is broken and acquired
  4. duplicate against a HEALTHY holder (real subprocess HTTP server)
     stands down (False) when not the intended successor
  5. intended successor takes over after the holder dies
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import dualstack_serve as ds

ROOT = Path(ds.__file__).resolve().parent.parent  # same root the module uses
LOCK_DIR = ROOT / ".brain" / "logs"


def _lock_path(port: int) -> Path:
    return LOCK_DIR / f"router-{port}.lock"


@pytest.fixture()
def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clear_lock(port: int) -> None:
    try:
        _lock_path(port).unlink()
    except FileNotFoundError:
        pass


# -- 1. basic acquisition -------------------------------------------------

def test_lock_acquired_contains_pid(free_port):
    _clear_lock(free_port)
    assert ds.acquire_instance_lock(free_port, max_wait_s=1.0) is True
    assert _lock_path(free_port).read_text().strip() == str(os.getpid())
    _clear_lock(free_port)


def test_lock_reentry_self_holder(free_port):
    _clear_lock(free_port)
    assert ds.acquire_instance_lock(free_port, max_wait_s=1.0) is True
    # Same process asking again = re-entry, still ours.
    assert ds.acquire_instance_lock(free_port, max_wait_s=1.0) is True
    _clear_lock(free_port)


# -- 3. stale lock with dead holder ----------------------------------------

def test_lock_stale_dead_holder_broken(free_port):
    _clear_lock(free_port)
    dead_pid = 999999997  # not a real PID
    _lock_path(free_port).write_text(str(dead_pid))
    assert ds._pid_alive(dead_pid) is False
    t0 = time.monotonic()
    assert ds.acquire_instance_lock(free_port, max_wait_s=5.0) is True
    assert time.monotonic() - t0 < 3.0  # broke immediately, no wait-out
    assert _lock_path(free_port).read_text().strip() == str(os.getpid())
    _clear_lock(free_port)


# -- 4. healthy holder: duplicate stands down ------------------------------

_SVR_SRC = r"""
import http.server, os, sys
port = int(sys.argv[1]); lock = sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
with open(lock, "w") as f:
    f.write(str(os.getpid()))
sys.stderr.write("READY\n"); sys.stderr.flush()
srv.serve_forever()
"""


_HOLDER_PROCS: list = []


def _spawn_healthy_holder(port: int) -> subprocess.Popen:
    lock = str(_lock_path(port))
    proc = subprocess.Popen(
        [sys.executable, "-c", _SVR_SRC, str(port), lock],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    _HOLDER_PROCS.append(proc)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _lock_path(port).exists() and ds._probe_health(port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"holder died: {proc.stderr.read()[:300]!r}")
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("holder never became healthy")


@pytest.fixture(autouse=True)
def _kill_leaked_holders():
    yield
    for p in _HOLDER_PROCS:
        try:
            p.kill()
        except Exception:
            pass
    _HOLDER_PROCS.clear()


def test_lock_duplicate_stands_down_vs_healthy_holder(free_port):
    _clear_lock(free_port)
    holder = _spawn_healthy_holder(free_port)
    try:
        # Not the intended successor -> immediate stand-down.
        t0 = time.monotonic()
        assert ds.acquire_instance_lock(free_port, intended_successor=False,
                                        max_wait_s=10.0) is False
        assert time.monotonic() - t0 < 5.0
        # Lock still owned by the holder.
        assert int(_lock_path(free_port).read_text()) == holder.pid
    finally:
        holder.kill(); holder.wait(timeout=5)
        _clear_lock(free_port)


def test_lock_successor_takes_over_after_death(free_port):
    _clear_lock(free_port)
    holder = _spawn_healthy_holder(free_port)
    # Duplicate stands down while holder is healthy.
    assert ds.acquire_instance_lock(free_port, intended_successor=False,
                                    max_wait_s=10.0) is False
    # Kill the holder; a successor (with a wait budget) must acquire soon
    # after the holder's death — it polls every _LOCK_RETRY_STEP_S.
    holder.kill(); holder.wait(timeout=5)
    t0 = time.monotonic()
    assert ds.acquire_instance_lock(free_port, intended_successor=True,
                                    max_wait_s=15.0) is True
    assert time.monotonic() - t0 < 10.0
    _clear_lock(free_port)
