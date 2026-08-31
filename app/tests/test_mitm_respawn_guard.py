"""Regression tests for the MITM respawn-supervisor guard.

Proves that kill_respawn_supervisors walks the parent chain of a port
listener and tree-kills a respawn supervisor (e.g. `while($true){mitmdump}`),
while leaving unrelated listening processes untouched.

Uses a throwaway high port (8443) so it never interferes with the live 443
listener. Tests are pure unit/mocks — no real processes are spawned or killed,
making them safe and deterministic in any pytest harness.
"""

import os
from unittest import mock

import pytest

from app.utils import mitm_kill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_taskkill(supervisor_pids):
    """Return a Mock subprocess.run that succeeds only for supervisor_pids.

    The returned Mock records every call (call_args / call_count) so tests can
    assert which PIDs were targeted, while behaving like taskkill /F /T.
    """
    def _run(cmd, **_kwargs):
        pid = int(cmd[cmd.index("/PID") + 1])
        ok = pid in supervisor_pids
        return mock.Mock(
            returncode=0 if ok else 1,
            stderr="" if ok else f"ERROR: The process with PID {pid} could not be found.",
            stdout="",
        )
    return mock.Mock(side_effect=_run)


# ---------------------------------------------------------------------------
# kill_respawn_supervisors — supervisor detection & kill
# ---------------------------------------------------------------------------

class TestKillRespawnSupervisors:
    """Supervisor is selected from the parent chain and tree-killed."""

    def test_kills_supervisor_not_just_child(self):
        """A powershell while-loop parent of a mitmdump listener is killed."""
        child_pid = 9001
        sup_pid = 8001
        sup_cmd = (
            "powershell -Command while($true){"
            "mitmdump --listen-port 8443; Start-Sleep -Seconds 1}"
        )

        with mock.patch.object(
            mitm_kill, "_get_listener_pids", return_value={child_pid}
        ), mock.patch.object(
            mitm_kill, "_get_process_info", return_value=(sup_pid, sup_cmd)
        ), mock.patch.object(
            # Critical-process guard (2026-08-31): the pre-scan must be able
            # to identify the supervisor as an ordinary powershell.exe.
            mitm_kill, "_get_process_names", return_value={sup_pid: "powershell.exe"}
        ), mock.patch.object(
            mitm_kill, "_is_protected_pid", return_value=False
        ), mock.patch.object(
            mitm_kill.subprocess, "run", _fake_taskkill({sup_pid})
        ) as mock_run:
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is True
        assert str(sup_pid) in detail
        # The supervisor was tree-killed (not the child directly).
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["taskkill", "/F", "/T"]
        assert str(sup_pid) in cmd

    def test_skips_unrelated_listener(self):
        """A listener whose parent is NOT a respawn loop is left alone."""
        child_pid = 9002
        parent_pid = 7002
        parent_cmd = "C:\\Windows\\System32\\svchost.exe -k netsvcs"

        with mock.patch.object(
            mitm_kill, "_get_listener_pids", return_value={child_pid}
        ), mock.patch.object(
            mitm_kill, "_get_process_info", return_value=(parent_pid, parent_cmd)
        ), mock.patch.object(
            mitm_kill, "_is_protected_pid", return_value=False
        ), mock.patch.object(
            mitm_kill.subprocess, "run", _fake_taskkill(set())
        ) as mock_run:
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is True
        assert "no respawn supervisors found" in detail.lower()
        mock_run.assert_not_called()

    def test_walks_parent_chain_up_to_depth(self):
        """Walk climbs the chain until it finds a supervisor, up to depth 6."""
        child_pid = 9003
        mid_pid = 8003   # non-supervisor middle node
        sup_pid = 7003   # supervisor higher up

        calls = {"depth": 0}

        def fake_info(pid):
            calls["depth"] += 1
            if pid == child_pid:
                return mid_pid, "python -m some_module"
            if pid == mid_pid:
                return sup_pid, "powershell -Command while($true){mitmdump --listen-port 8443; Start-Sleep 1}"
            return None, None

        with mock.patch.object(
            mitm_kill, "_get_listener_pids", return_value={child_pid}
        ), mock.patch.object(
            mitm_kill, "_get_process_info", side_effect=fake_info
        ), mock.patch.object(
            # Critical-process guard (2026-08-31): identify the flagged
            # supervisor as an ordinary powershell.exe so the pre-scan passes.
            mitm_kill, "_get_process_names", return_value={sup_pid: "powershell.exe"}
        ), mock.patch.object(
            mitm_kill, "_is_protected_pid", return_value=False
        ), mock.patch.object(
            mitm_kill.subprocess, "run", _fake_taskkill({sup_pid})
        ) as mock_run:
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is True
        assert str(sup_pid) in detail
        # Walked child -> mid (lookup 1) -> sup (lookup 2, flagged, break).
        assert calls["depth"] == 2
        cmd = mock_run.call_args[0][0]
        assert str(sup_pid) in cmd

    def test_stops_at_protected_pid(self):
        """Walk does not cross PID 4 (System) or PID 0 boundaries."""
        child_pid = 9004
        protected_ppid = 4  # System — protected

        with mock.patch.object(
            mitm_kill, "_get_listener_pids", return_value={child_pid}
        ), mock.patch.object(
            mitm_kill, "_get_process_info",
            return_value=(protected_ppid, "some cmdline with while and mitmdump")
        ), mock.patch.object(
            mitm_kill, "_is_protected_pid",
            side_effect=lambda pid: pid <= 4 or pid == os.getpid()
        ), mock.patch.object(
            mitm_kill.subprocess, "run", _fake_taskkill(set())
        ) as mock_run:
            ok, _detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is True
        mock_run.assert_not_called()

    def test_no_listeners_returns_ok(self):
        """Empty listener set is a clean success."""
        with mock.patch.object(
            mitm_kill, "_get_listener_pids", return_value=set()
        ):
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)
        assert ok is True
        assert "nothing to scan" in detail.lower()

    def test_supervisor_taskkill_failure_propagates(self):
        """A failing taskkill against the supervisor makes ok=False."""
        child_pid = 9005
        sup_pid = 8005
        sup_cmd = "powershell -Command while($true){mitmdump --listen-port 8443; Start-Sleep 1}"

        def fail_run(cmd, **_kwargs):
            return mock.Mock(returncode=1, stderr="Access is denied.", stdout="")

        with mock.patch.object(
            mitm_kill, "_get_listener_pids", return_value={child_pid}
        ), mock.patch.object(
            mitm_kill, "_get_process_info", return_value=(sup_pid, sup_cmd)
        ), mock.patch.object(
            mitm_kill, "_is_protected_pid", return_value=False
        ), mock.patch.object(mitm_kill.subprocess, "run", fail_run):
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is False
        assert str(sup_pid) in detail


# ---------------------------------------------------------------------------
# force_kill_mitm_port — supervisor pre-step integration
# ---------------------------------------------------------------------------

class TestForceKillPreStep:
    """force_kill_mitm_port invokes kill_respawn_supervisors BEFORE the
    listener kill loop, preserving all existing safety guards."""

    def test_supervisor_guard_runs_before_listener_loop(self):
        """The respawn guard is called as a pre-step, then the 3-round loop."""
        call_order = []

        def fake_guard(port):
            call_order.append(("guard", port))
            return True, "no supervisors"

        def fake_get_pids(port):
            call_order.append(("get_pids", port))
            return set()  # clear on first round

        with mock.patch.object(
            mitm_kill, "kill_respawn_supervisors", side_effect=fake_guard
        ), mock.patch.object(
            mitm_kill, "_get_listener_pids", side_effect=fake_get_pids
        ):
            ok, _detail = mitm_kill.force_kill_mitm_port(443)

        assert ok is True
        # Guard ran BEFORE any listener scan.
        assert call_order[0] == ("guard", 443)
        assert call_order[1] == ("get_pids", 443)

    def test_guard_failure_does_not_abort_listener_loop(self):
        """Even if the supervisor guard partially fails, the listener kill
        retry loop still runs (defence in depth)."""
        def fake_guard(port):
            return False, "one supervisor failed to kill"

        rounds = {"n": 0}

        def fake_get_pids(port):
            rounds["n"] += 1
            return set()  # clear

        with mock.patch.object(
            mitm_kill, "kill_respawn_supervisors", side_effect=fake_guard
        ), mock.patch.object(
            mitm_kill, "_get_listener_pids", side_effect=fake_get_pids
        ):
            ok, _detail = mitm_kill.force_kill_mitm_port(443)

        # Still succeeded because the port cleared, despite guard failure.
        assert ok is True
        # At least one round of the listener loop ran.
        assert rounds["n"] >= 1
