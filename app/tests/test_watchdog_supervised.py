"""Unit tests for the B1 supervisor (app.watchdog, rework 2026-08-27).

Covers the lifecycle contract:
  1. spawn goes through `-m app.dualstack_serve` with BSL_SUPERVISED=1
  2. child crash (nonzero exit) → respawn
  3. child clean exit (code 0) → supervisor stands down, NO respawn
  4. health freeze (3 consecutive fails) → kill + respawn
  5. crash-loop cap: MAX_RESTARTS_PER_WINDOW restarts → GIVING UP
  6. TCP boot gate gates probing (port never opens → still supervised, no kill)

All subprocess/health/time interactions are mocked — no real router spawns.
"""

from __future__ import annotations

import subprocess
import threading
import time
from unittest import mock

import pytest

import app.watchdog as wd


class FakeChild:
    """Mimics subprocess.Popen for the supervisor loop."""

    def __init__(self, exit_code: int | None = None):
        self.pid = 4242
        self._exit_code = exit_code  # None = still running
        self.returncode = None
        self.killed = False

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.killed = True
        self._exit_code = 0
        self.returncode = 0

    def kill(self):
        self.killed = True
        self._exit_code = 9
        self.returncode = 9

    def wait(self, timeout=None):
        return self._exit_code if self._exit_code is not None else 0


def _run_until_return(cap_respawns: int = 0, **kwargs):
    """Drive run_supervised with everything mocked; return call record.

    ``cap_respawns``: after this many respawns, force signals/loop exit so
    tests don't spin forever on a healthy child.
    """
    spawns: list[dict] = []
    kills: list[int] = []
    healthy_probes: list[bool] = []
    port_checks: list[int] = []

    def fake_spawn(port):
        spawns.append({"port": port})
        return FakeChild()

    def fake_kill(child):
        kills.append(child.pid)

    def fake_health(port):
        healthy_probes.append(True)
        return True

    def fake_wait_port(port, host="127.0.0.1", timeout_s=None):
        port_checks.append(port)
        return True

    with mock.patch.object(wd, "_spawn_child", side_effect=fake_spawn), \
         mock.patch.object(wd, "_kill_child", side_effect=fake_kill), \
         mock.patch.object(wd, "_health_check", side_effect=fake_health), \
         mock.patch.object(wd, "_wait_for_port", side_effect=fake_wait_port), \
         mock.patch.object(wd.signal, "signal", lambda *a, **k: None), \
         mock.patch.object(wd, "HEALTH_POLL_INTERVAL_S", 0.0), \
         mock.patch.object(wd, "RESTART_COOLDOWN_S", 0.0):
        try:
            wd.run_supervised(**kwargs)
        except _StopTest:
            pass
    return spawns, kills, healthy_probes, port_checks


class _StopTest(Exception):
    """Raised by tests to break out of the supervisor loop."""


def test_spawn_uses_dualstack_entrypoint_with_env_guard():
    """_spawn_child must spawn `-m app.dualstack_serve` + set BSL_SUPERVISED=1."""
    recorded: dict = {}

    def fake_popen(cmd, cwd=None, env=None, **kwargs):
        recorded["cmd"] = cmd
        recorded["cwd"] = cwd
        recorded["env"] = env
        child = mock.MagicMock()
        child.pid = 12345
        return child

    with mock.patch.object(wd.subprocess, "Popen", side_effect=fake_popen):
        fake_child = mock.MagicMock()
        fake_child.pid = 12345
        # rewire: popen returns our explicit mock so .pid exists
        fake_popen_result = fake_child
        recorded["child"] = fake_popen_result
        wd._spawn_child(6969)

    assert recorded["cmd"][-3:] == ["-m", "app.dualstack_serve", "--port", "6969"][-3:] or \
           recorded["cmd"] == [wd.sys.executable, "-m", "app.dualstack_serve", "--port", "6969"]
    assert recorded["env"]["BSL_SUPERVISED"] == "1"
    assert "dualstack_serve" in " ".join(recorded["cmd"])


def test_child_crash_triggers_respawn():
    """Nonzero child exit → supervisor must respawn (the old code exited)."""
    respawns = {"n": 0}
    crash_then_healthy: list[FakeChild] = [
        FakeChild(exit_code=None),  # first child: healthy forever
    ]
    # First child crashes after first poll; second child stays healthy and we
    # break the loop after 2 spawns.
    crash_then_healthy[0]._exit_code = 1  # simulate crash detection on poll

    def fake_spawn(port):
        respawns["n"] += 1
        if respawns["n"] == 1:
            return FakeChild(exit_code=1)  # already dead → crash path
        # second spawn: healthy running child; break loop on next health poll
        child = FakeChild(exit_code=None)
        return child

    probes = {"n": 0}

    def fake_health(port):
        probes["n"] += 1
        if probes["n"] > 2:
            raise _StopTest  # healthy loop proven; stop test
        return True

    with mock.patch.object(wd, "_spawn_child", side_effect=fake_spawn), \
         mock.patch.object(wd, "_kill_child", lambda child: None), \
         mock.patch.object(wd, "_health_check", side_effect=fake_health), \
         mock.patch.object(wd, "_wait_for_port", return_value=True), \
         mock.patch.object(wd.signal, "signal", lambda *a, **k: None), \
         mock.patch.object(wd, "HEALTH_POLL_INTERVAL_S", 0.0), \
         mock.patch.object(wd, "RESTART_COOLDOWN_S", 0.0):
        with pytest.raises(_StopTest):
            wd.run_supervised(port=6969)

    assert respawns["n"] == 2, "a crashed child must be respawned exactly once"


def test_child_clean_exit_stands_down():
    """Exit code 0 = graceful path → supervisor returns, never respawns."""
    spawns: list = []

    def fake_spawn(port):
        spawns.append(1)
        return FakeChild(exit_code=0)  # exited cleanly immediately

    with mock.patch.object(wd, "_spawn_child", side_effect=fake_spawn), \
         mock.patch.object(wd, "_wait_for_port", return_value=True), \
         mock.patch.object(wd, "MAX_CONSECUTIVE_FAILURES", 3):
        wd.run_supervised(port=6969)  # must return without raising

    assert len(spawns) == 1, "clean exit must NOT respawn (new instance handles it)"


def test_health_freeze_kills_and_respawns():
    """3 consecutive health fails → _kill_child then respawn."""
    spawns = {"n": 0}
    freeze_seq = [False, False, False, True]  # 3 fails then recovery

    def fake_spawn(port):
        spawns["n"] += 1
        return FakeChild(exit_code=None)

    killed = {"n": 0}
    state = {"probes": 0}

    def fake_health(port):
        state["probes"] += 1
        if state["probes"] <= 3:
            return False
        # after respawn, probe stays healthy; break loop soon
        if state["probes"] >= 5:
            raise _StopTest
        return True

    def fake_kill(child):
        killed["n"] += 1

    with mock.patch.object(wd, "_spawn_child", side_effect=fake_spawn), \
         mock.patch.object(wd, "_kill_child", side_effect=fake_kill), \
         mock.patch.object(wd, "_health_check", side_effect=fake_health), \
         mock.patch.object(wd, "_wait_for_port", return_value=True), \
         mock.patch.object(wd.signal, "signal", lambda *a, **k: None), \
         mock.patch.object(wd, "HEALTH_POLL_INTERVAL_S", 0.0), \
         mock.patch.object(wd, "RESTART_COODLDOWN_S" if hasattr(wd, "RESTART_COODLDOWN_S") else "RESTART_COOLDOWN_S", 0.0):
        with pytest.raises(_StopTest):
            wd.run_supervised(port=6969)

    assert killed["n"] == 1, "frozen child must be killed exactly once"
    assert spawns["n"] == 2, "frozen child must be respawned exactly once"


def test_crash_loop_cap_gives_up():
    """After MAX_RESTARTS_PER_WINDOW restarts in the window → GIVING UP (return)."""
    spawns = {"n": 0}

    def fake_spawn(port):
        spawns["n"] += 1
        return FakeChild(exit_code=1)  # always crash instantly

    with mock.patch.object(wd, "_spawn_child", side_effect=fake_spawn), \
         mock.patch.object(wd, "_kill_child", lambda child: None), \
         mock.patch.object(wd, "_wait_for_port", return_value=True), \
         mock.patch.object(wd, "HEALTH_POLL_INTERVAL_S", 0.0), \
         mock.patch.object(wd, "RESTART_COOLDOWN_S", 0.0):
        wd.run_supervised(port=6969)  # must return (GIVING UP), not loop forever

    # Flow: 1 initial spawn + MAX_RESTARTS_PER_WINDOW respawned crashers =
    # cap+1 total spawns, then GIVING UP returns (test reaching here at all
    # proves the cap fired — an uncapped loop would hang forever).
    assert spawns["n"] == wd.MAX_RESTARTS_PER_WINDOW + 1, \
        "supervisor must stop after exactly the cap, even with instant crashes"


def test_boot_gate_failure_still_supervises_without_killing():
    """Port never opens (zombie bind-retry) → no kill yet; loop continues."""
    spawns = {"n": 0}

    def fake_spawn(port):
        spawns["n"] += 1
        # healthy but port closed: child alive, freeze must NOT trigger before
        # port opens — here we let the FIRST boot gate fail then break loop.
        return FakeChild(exit_code=None)

    calls = {"wait_port": 0, "health": 0}

    def fake_wait_port(port, host="127.0.0.1", timeout_s=None):
        calls["wait_port"] += 1
        return False  # port never opens

    def fake_health(port):
        calls["health"] += 1
        if calls["health"] >= 2:
            raise _StopTest
        return False  # probes fail (port closed) but child is alive

    killed = {"n": 0}

    with mock.patch.object(wd, "_spawn_child", side_effect=fake_spawn), \
         mock.patch.object(wd, "_kill_child", lambda child: killed.__setitem__("n", killed["n"] + 1)), \
         mock.patch.object(wd, "_health_check", side_effect=fake_health), \
         mock.patch.object(wd, "_wait_for_port", side_effect=fake_wait_port), \
         mock.patch.object(wd.signal, "signal", lambda *a, **k: None), \
         mock.patch.object(wd, "HEALTH_POLL_INTERVAL_S", 0.0), \
         mock.patch.object(wd, "RESTART_COOLDOWN_S", 0.0):
        with pytest.raises(_StopTest):
            wd.run_supervised(port=6969)

    # boot gate ran, health probes only STARTED after gate failed (gate is
    # non-fatal), and with only 2 probes (< 3 needed) no kill happened.
    assert calls["wait_port"] >= 1
    assert calls["health"] == 2
    assert killed["n"] == 0, "child in boot-gate timeout must not be killed prematurely"


def test_wait_for_port_timeout_returns_false():
    """TCP gate: closed port → False after timeout (no infinite hang)."""
    with mock.patch.object(wd.socket, "create_connection",
                           side_effect=OSError("refused")), \
         mock.patch.object(wd.time, "sleep", lambda s: None), \
         mock.patch.object(wd, "CHILD_BOOT_TIMEOUT_S", 0.05):
        assert wd._wait_for_port(6969, timeout_s=0.05) is False
