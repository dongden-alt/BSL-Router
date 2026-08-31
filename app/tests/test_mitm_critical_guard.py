"""Regression tests for the critical-process kill guard (0xEF prevention).

2026-08-31 RCA: a MITM port eviction could taskkill the Windows System
process (PID 4, which HTTP.sys uses to bind :443) or another protected /
critical process, bugchecking the machine (CRITICAL_PROCESS_DIED, 0xEF).
These tests prove force_kill_mitm_port and kill_respawn_supervisors now
refuse such kills loudly instead of executing them, while ordinary foreign
owners (e.g. 9Router's node.exe) are still evicted normally.

Pure mocks — no real processes are spawned, killed, or queried.
"""

from unittest import mock

import pytest

from app.utils import mitm_kill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_taskkill_recorder():
    """subprocess.run mock that records every command and always succeeds."""
    calls = []

    def _run(cmd, **_kwargs):
        calls.append(list(cmd))
        return mock.Mock(returncode=0, stderr="", stdout="")

    return mock.Mock(side_effect=_run), calls


# ---------------------------------------------------------------------------
# _is_critical_process_name
# ---------------------------------------------------------------------------

class TestCriticalNameClassification:
    def test_protected_and_system_host_names_are_critical(self):
        for name in (
            "svchost", "SVCHOST.EXE", "csrss.exe", "Services", "System",
            "wininit.exe", "lsass", "explorer.exe", "dwm", "conhost",
        ):
            assert mitm_kill._is_critical_process_name(name) is True, name

    def test_ordinary_listener_names_are_not_critical(self):
        for name in ("node", "node.exe", "python", "python.exe", "mitmdump.exe", "chrome"):
            assert mitm_kill._is_critical_process_name(name) is False, name

    def test_empty_name_is_not_critical(self):
        assert mitm_kill._is_critical_process_name("") is False
        assert mitm_kill._is_critical_process_name(None) is False


# ---------------------------------------------------------------------------
# _get_process_names
# ---------------------------------------------------------------------------

class TestGetProcessNames:
    def test_parses_wmi_output(self):
        fake = mock.Mock(returncode=0, stdout="1234|node.exe\n5678|svchost.exe\n\n", stderr="")
        with mock.patch.object(mitm_kill.subprocess, "run", return_value=fake):
            names = mitm_kill._get_process_names({1234, 5678})
        assert names == {1234: "node.exe", 5678: "svchost.exe"}

    def test_empty_input_skips_subprocess(self):
        with mock.patch.object(mitm_kill.subprocess, "run") as run_mock:
            assert mitm_kill._get_process_names(set()) == {}
        run_mock.assert_not_called()

    def test_wmi_failure_raises(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="access denied")
        with mock.patch.object(mitm_kill.subprocess, "run", return_value=fake):
            with pytest.raises(RuntimeError):
                mitm_kill._get_process_names({1234})


# ---------------------------------------------------------------------------
# force_kill_mitm_port — critical-process pre-scan
# ---------------------------------------------------------------------------

class TestForceKillCriticalGuard:
    def test_refuses_kill_of_critical_listener(self):
        """A svchost-owned :443 is refused; taskkill is NEVER issued."""
        run_mock, calls = _mock_taskkill_recorder()
        with mock.patch.object(mitm_kill, "_get_listener_pids", return_value={4444}), \
             mock.patch.object(mitm_kill, "_get_process_names", return_value={4444: "svchost.exe"}), \
             mock.patch.object(mitm_kill, "kill_respawn_supervisors", return_value=(True, "none")), \
             mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, detail = mitm_kill.force_kill_mitm_port(443)

        assert ok is False
        assert "critical" in detail.lower()
        assert "4444" in detail
        assert calls == []

    def test_normal_foreign_listener_still_evicted(self):
        """A plain node.exe owner is killed exactly as before the guard."""
        run_mock, calls = _mock_taskkill_recorder()
        with mock.patch.object(
            mitm_kill, "_get_listener_pids", side_effect=[{5555}, set()]
        ), mock.patch.object(
            mitm_kill, "_get_process_names", return_value={5555: "node.exe"}
        ), mock.patch.object(
            mitm_kill, "kill_respawn_supervisors", return_value=(True, "none")
        ), mock.patch.object(
            mitm_kill.time, "sleep"
        ), mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, detail = mitm_kill.force_kill_mitm_port(443)

        assert ok is True
        assert len(calls) == 1
        assert calls[0][:3] == ["taskkill", "/F", "/T"]
        assert "5555" in calls[0]

    def test_name_lookup_failure_fails_closed(self):
        """If WMI is broken, a LIVE listener is never killed blind."""
        run_mock, calls = _mock_taskkill_recorder()
        with mock.patch.object(mitm_kill, "_get_listener_pids", return_value={6666}), \
             mock.patch.object(mitm_kill, "_get_process_names", side_effect=RuntimeError("WMI down")), \
             mock.patch.object(mitm_kill, "kill_respawn_supervisors", return_value=(True, "none")), \
             mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, detail = mitm_kill.force_kill_mitm_port(443)

        assert ok is False
        assert calls == []

    def test_dead_listener_without_name_still_cleaned(self):
        """A PID that vanished between netstat and the lookup (no error, no
        name) proceeds to taskkill — which then no-ops as 'not found'."""
        run_mock, calls = _mock_taskkill_recorder()
        with mock.patch.object(
            mitm_kill, "_get_listener_pids", side_effect=[{7777}, set()]
        ), mock.patch.object(
            mitm_kill, "_get_process_names", return_value={}
        ), mock.patch.object(
            mitm_kill, "kill_respawn_supervisors", return_value=(True, "none")
        ), mock.patch.object(
            mitm_kill.time, "sleep"
        ), mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, _detail = mitm_kill.force_kill_mitm_port(443)

        assert ok is True
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# kill_respawn_supervisors — critical-process pre-scan
# ---------------------------------------------------------------------------

class TestSupervisorCriticalGuard:
    def test_critical_supervisor_refused(self):
        """A flagged supervisor whose name is svchost is never tree-killed."""
        run_mock, calls = _mock_taskkill_recorder()
        sup_cmd = "powershell -Command while($true){mitmdump --listen-port 8443; Start-Sleep 1}"
        with mock.patch.object(mitm_kill, "_get_listener_pids", return_value={9001}), \
             mock.patch.object(mitm_kill, "_get_process_info", return_value=(8001, sup_cmd)), \
             mock.patch.object(mitm_kill, "_is_protected_pid", return_value=False), \
             mock.patch.object(mitm_kill, "_get_process_names", return_value={8001: "svchost.exe"}), \
             mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is False
        assert "critical" in detail.lower()
        assert calls == []

    def test_normal_supervisor_still_killed(self):
        """A powershell respawn loop with an ordinary name is killed as before."""
        run_mock, calls = _mock_taskkill_recorder()
        sup_cmd = "powershell -Command while($true){mitmdump --listen-port 8443; Start-Sleep 1}"
        with mock.patch.object(mitm_kill, "_get_listener_pids", return_value={9002}), \
             mock.patch.object(mitm_kill, "_get_process_info", return_value=(8002, sup_cmd)), \
             mock.patch.object(mitm_kill, "_is_protected_pid", return_value=False), \
             mock.patch.object(mitm_kill, "_get_process_names", return_value={8002: "powershell.exe"}), \
             mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is True
        assert len(calls) == 1
        assert calls[0][:3] == ["taskkill", "/F", "/T"]

    def test_lookup_failure_on_supervisor_fails_closed(self):
        run_mock, calls = _mock_taskkill_recorder()
        sup_cmd = "powershell -Command while($true){mitmdump --listen-port 8443; Start-Sleep 1}"
        with mock.patch.object(mitm_kill, "_get_listener_pids", return_value={9003}), \
             mock.patch.object(mitm_kill, "_get_process_info", return_value=(8003, sup_cmd)), \
             mock.patch.object(mitm_kill, "_is_protected_pid", return_value=False), \
             mock.patch.object(mitm_kill, "_get_process_names", side_effect=RuntimeError("WMI down")), \
             mock.patch.object(mitm_kill.subprocess, "run", run_mock):
            ok, _detail = mitm_kill.kill_respawn_supervisors(8443)

        assert ok is False
        assert calls == []
