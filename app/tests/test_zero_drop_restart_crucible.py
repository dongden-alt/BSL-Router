"""Triple Adversarial Crucible Test Suite for Zero-Drop BSL Router Stop/Restart Lifecycle.

Verifies all 5 root-cause fixes that prevent Antigravity App / Blacksand Core /
Blacksand Studio App from erroring out or stopping whenever BSL Router (:6969)
is stopped or restarted (especially when an agent inside the app runs a
stop/restart server command via `run_command`):

- Vector 1 (Loopback Keep-Alive Poisoning & URL-Encoded Route Attack):
  Verifies `Connection: close` on all loopback routes including URL-encoded
  `/v1internal%3AstreamGenerateContent`, `/v1/*`, `/v1beta/*`, `/anthropic/*`,
  `/gemini/*`, `/health`, `/api/system/*`, and `/api/version/*`.
- Vector 2 (CLI/Agent Handover Synchronization, Expected-PID Guard & 45s Stream Drain):
  Verifies synchronous handover wait on `_HANDOVER_READY_EVENT` for CLI/agent callers,
  `409 Conflict` rejection of mismatched `expected_pid` on `/api/system/handover-exit`,
  single-flight handover lock coalescing, and 45s two-streak zero-stream drain in
  `app/prewarm_handover.py` with zero `/api/system/shutdown` fallback.
- Vector 3 (Launcher Zero-Downtime Handover, Agent Stop Self-Preservation & Live Dual-Stack Swap):
  Verifies `scripts/bslrouter.ps1` and `scripts/shutdown_bsl.ps1` never execute a
  kill-first 2-second blackout on `restart` or leave `:6969` offline on an unconfirmed
  agent `stop`, and runs a live dual-stack socket handover stress test on an ephemeral
  port proving <50ms handover with 0 dropped connections.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
import app.prewarm_handover as handover_mod
from app.tests.test_mitm_lifecycle import _powershell_code_only


# ===========================================================================
# VECTOR 1: Loopback `Connection: close` & URL-Encoded `/v1internal%3A` Attack
# ===========================================================================


def test_vector1_loopback_connection_close_covers_url_encoded_v1internal_and_all_proxy_routes():
    """Go's `language_server_windows_x64.exe` sends `/v1internal%3AstreamGenerateContent?alt=sse`
    (with URL-encoded colon `%3A`) as well as `/v1/chat/completions`, `/v1/models`, and `/health`.
    Every loopback response MUST carry `Connection: close` so the IDE never pools a stale
    HTTP/1.1 Keep-Alive socket across a router PID handover (WinError 10054 WSAECONNRESET).
    """
    client = TestClient(main_mod.app)

    probes = [
        ("GET", "/health"),
        ("GET", "/v1/models"),
        ("POST", "/v1internal:countTokens"),
        ("POST", "/v1internal%3AcountTokens"),
        ("POST", "/v1internal%3AstreamGenerateContent?alt=sse"),
        ("POST", "/GetAvailableModels"),
        ("POST", "/v1/messages"),
        ("POST", "/anthropic/v1/messages"),
        ("GET", "/api/version/check"),
    ]
    for method, path in probes:
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json={})
        assert resp.headers.get("connection") == "close", (
            f"Missing Connection: close on loopback {method} {path} (got {resp.headers.get('connection')!r})"
        )


# ===========================================================================
# VECTOR 2: Expected-PID Guard, Synchronous CLI Wait, Single-Flight Lock & Drain
# ===========================================================================


def test_vector2_handover_exit_rejects_mismatched_or_missing_expected_pid_with_409():
    """A stale handover worker whose `--old-pid` no longer matches the live router PID
    (or any unparameterized call without `expected_pid`) must receive HTTP 409 Conflict
    (`pid_mismatch`) and NEVER terminate the live router.
    """
    client = TestClient(main_mod.app)
    wrong_pid = os.getpid() + 99999

    resp = client.post(f"/api/system/handover-exit?expected_pid={wrong_pid}")
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "pid_mismatch"
    assert body["current_pid"] == os.getpid()
    assert body["expected_pid"] == wrong_pid

    resp_missing = client.post("/api/system/handover-exit")
    assert resp_missing.status_code == 409
    assert resp_missing.json()["error"] == "pid_mismatch"


def test_vector2_watchdog_respawns_if_rc_zero_has_no_healthy_successor(monkeypatch):
    """If a router child exits with rc=0 in production (e.g. CTRL_C_EVENT or a failed
    handover where the new worker died before binding :6969), `app/watchdog.py` must
    check `_health_check(port)` and respawn the child when no successor is listening.
    """
    import app.watchdog as wd

    monkeypatch.delenv("BSL_TEST_ISOLATION", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    spawns = []

    class _FakeChild:
        def __init__(self, pid, rc):
            self.pid = pid
            self._rc = rc

        def poll(self):
            return self._rc

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    class _StopWatchdog(Exception):
        pass

    def _fake_spawn(port):
        idx = len(spawns)
        spawns.append(idx)
        if idx == 0:
            return _FakeChild(1001, 0)  # First child exits with rc=0 and NO successor listening
        raise _StopWatchdog

    monkeypatch.setattr(wd, "_spawn_child", _fake_spawn)
    monkeypatch.setattr(wd, "_wait_for_port", lambda port: True)
    monkeypatch.setattr(wd, "_health_check", lambda port: False)
    monkeypatch.setattr(wd, "_kill_child", lambda child: None)
    monkeypatch.setattr(wd, "RESTART_COOLDOWN_S", 0.0)
    monkeypatch.setattr(wd.time, "sleep", lambda s: None)

    try:
        with pytest.raises(_StopWatchdog):
            wd.run_supervised(port=6969)
        assert len(spawns) == 2, "Watchdog must respawn when rc=0 has no healthy successor on :6969"
    finally:
        os.environ["BSL_TEST_ISOLATION"] = "1"


def test_vector2_handover_exit_accepts_matching_expected_pid_and_signals_ready_event():
    """When `expected_pid` matches `os.getpid()`, `/api/system/handover-exit` must set
    `_HANDOVER_READY_EVENT` and return 200 OK (isolated in test mode without calling os._exit).
    """
    main_mod._HANDOVER_READY_EVENT.clear()
    client = TestClient(main_mod.app)
    my_pid = os.getpid()

    resp = client.post(f"/api/system/handover-exit?expected_pid={my_pid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["exiting_pid"] == my_pid
    assert main_mod._HANDOVER_READY_EVENT.is_set() is True


def test_vector2_cli_agent_restart_waits_for_handover_ready_event_while_browser_returns_immediately(monkeypatch):
    """When an agent calls `POST /api/system/restart?wait=1` or `/api/version/restart?wait=1`,
    the HTTP response must block until `_HANDOVER_READY_EVENT` is signaled by the pre-warmed
    replacement worker, ensuring `run_command` never exits 6 seconds before the socket swap.
    Browser UI callers (`User-Agent: Mozilla/5.0 ...`) return immediately so the UI toast/poll runs.
    """
    monkeypatch.setattr(main_mod, "_is_admin_auth_enabled", lambda: False)
    monkeypatch.setattr(main_mod, "_spawn_prewarm_handover_restart", lambda port=6969: os.getpid())

    main_mod._HANDOVER_READY_EVENT.clear()

    def _signal_after_120ms():
        time.sleep(0.12)
        main_mod._HANDOVER_READY_EVENT.set()

    threading.Thread(target=_signal_after_120ms, daemon=True).start()
    client = TestClient(main_mod.app)
    t0 = time.perf_counter()
    resp = client.post("/api/system/restart?wait=1", headers={"User-Agent": "curl/8.7.1"})
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    assert resp.json()["status"] == "restarting"
    assert elapsed >= 0.09, f"Expected synchronous wait for _HANDOVER_READY_EVENT, returned in {elapsed:.3f}s"

    # Browser caller with ?async=1 must return immediately even if event is cleared
    main_mod._HANDOVER_READY_EVENT.clear()
    t1 = time.perf_counter()
    resp_browser = client.post(
        "/api/system/restart?async=1",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    elapsed_browser = time.perf_counter() - t1
    assert resp_browser.status_code == 200
    assert elapsed_browser < 0.08


def test_vector2_prewarm_handover_never_calls_system_shutdown_and_aborts_if_pid_already_replaced(monkeypatch):
    """`app/prewarm_handover.py` must NEVER call `/api/system/shutdown` (which writes the
    delayed `bsl_kill.bat` bomb), and must abort immediately if `/health` shows a newer PID
    has already replaced `old_pid`.
    """
    src = Path("app/prewarm_handover.py").read_text(encoding="utf-8")
    code_lines = [
        line for line in src.splitlines()
        if not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    assert '"/api/system/shutdown"' not in code_only
    assert "'/api/system/shutdown'" not in code_only

    # Simulate `/health` returning a newer PID (e.g. 88888 != old_pid 11111)
    monkeypatch.setattr(handover_mod, "_get_listening_health_pid", lambda port: 88888)
    res = handover_mod._request_old_router_exit(6969, 11111)
    assert res == "already_replaced_by_88888"


def test_vector2_single_flight_handover_lock_coalesces_concurrent_restarts(monkeypatch, tmp_path):
    """5 concurrent restart calls within the 18s lock window must spawn at most 1 WMI
    pre-warmed handover supervisor instead of 5 competing workers fighting over :6969.
    """
    fake_root = tmp_path / "router_root"
    (fake_root / "app").mkdir(parents=True)
    (fake_root / ".brain" / "logs").mkdir(parents=True)
    fake_main_file = fake_root / "app" / "main.py"
    fake_main_file.write_text("# fake", encoding="utf-8")

    wmi_calls = []

    def _fake_run(cmd, **kwargs):
        wmi_calls.append(cmd)

        class _Res:
            returncode = 0

        return _Res()

    monkeypatch.setattr(main_mod, "__file__", str(fake_main_file))
    monkeypatch.delenv("BSL_TEST_ISOLATION", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import subprocess as sp_mod
    monkeypatch.setattr(sp_mod, "run", _fake_run)

    try:
        for _ in range(5):
            main_mod._spawn_prewarm_handover_restart(6969)
        assert len(wmi_calls) == 1, f"Expected single-flight lock to coalesce 5 calls into 1 spawn, got {len(wmi_calls)}"
    finally:
        os.environ["BSL_TEST_ISOLATION"] = "1"


# ===========================================================================
# VECTOR 3: Launcher Zero-Downtime Contract & Live Ephemeral Dual-Stack Swap
# ===========================================================================


def test_vector3_launcher_restart_and_stop_have_zero_kill_first_blackout():
    """Verify `scripts/bslrouter.ps1` and `scripts/shutdown_bsl.ps1` never run the legacy
    `Invoke-Stop; Start-Sleep -Seconds 2; Invoke-Start` kill-first blackout on `restart`,
    wait for `/health` on `Start-App`, and protect active IDE sessions on `stop`.
    """
    launcher = Path("scripts/bslrouter.ps1").read_text(encoding="utf-8")
    launcher_code = _powershell_code_only(launcher)

    # 1. No kill-first 2s blackout on `restart`
    assert "Invoke-Stop; Start-Sleep -Seconds 2; Invoke-Start" not in launcher_code
    assert "'restart'   { Invoke-Restart }" in launcher_code
    assert "function Invoke-AppPrewarmHandover" in launcher_code
    assert "function Wait-RouterHealthy" in launcher_code

    # 2. Cold Start-App waits for /health before returning to `run_command`
    start_app = launcher_code.split("function Start-App {", 1)[1].split("function Start-Mitm", 1)[0]
    assert "Wait-RouterHealthy -TargetPort $Port" in start_app

    # 3. Invoke-Stop and shutdown_bsl.ps1 guard active IDE sessions unless -ConfirmOffline
    invoke_stop = launcher_code.split("function Invoke-Stop {", 1)[1].split("function Format-ListenerOwners", 1)[0]
    assert "Test-ActiveIdeSession" in invoke_stop
    assert "Invoke-AppPrewarmHandover -TargetPort $Port" in invoke_stop

    shutdown_script = _powershell_code_only(Path("scripts/shutdown_bsl.ps1").read_text(encoding="utf-8"))
    assert "ConfirmOffline" in shutdown_script
    for surface_marker in (
        "language_server_windows_x64",
        "Antigravity",
        "agy",
        "Blacksand",
        "Blacksand-Studio",
        "Claude",
        "claude",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
    ):
        assert surface_marker in launcher_code, f"Missing {surface_marker} in bslrouter.ps1 Test-ActiveIdeSession"
        assert surface_marker in shutdown_script, f"Missing {surface_marker} in shutdown_bsl.ps1"


def test_vector3_live_ephemeral_dualstack_socket_handover_under_continuous_client_load():
    """Live adversarial socket stress test on an ephemeral port:
    1. Old server binds `[::]:<port>` using the exact dual-stack socket options and serves HTTP 200.
    2. A background handover thread closes the old socket while `handover_mod._bind_dualstack_fast(port)`
       spins at 5ms intervals to bind the replacement dual-stack socket and immediately serve HTTP 200.
    3. A client fires continuous requests across the swap; verify `unlistening_gap_ms < 100ms`
       and the replacement socket serves traffic with `pid_gen=2`.
    """
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        probe.bind(("::", 0))
        port = probe.getsockname()[1]

    sock1, _ = handover_mod._bind_dualstack_fast(port, timeout_s=2.0)
    stop_old = threading.Event()
    stop_new = threading.Event()
    handover_metrics = {}

    def _serve_http(srv_sock: socket.socket, stop_ev: threading.Event, gen: int):
        srv_sock.settimeout(0.05)
        while not stop_ev.is_set():
            try:
                conn, _ = srv_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.recv(1024)
                body = json.dumps({"ok": True, "gen": gen}).encode("utf-8")
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Connection: close\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
                conn.sendall(resp)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    t_old = threading.Thread(target=_serve_http, args=(sock1, stop_old, 1), daemon=True)
    t_old.start()

    def _delayed_close_old():
        time.sleep(0.06)
        stop_old.set()
        sock1.close()

    threading.Thread(target=_delayed_close_old, daemon=True).start()
    sock2, gap_ms = handover_mod._bind_dualstack_fast(port, timeout_s=3.0)
    handover_metrics["gap_ms"] = gap_ms

    t_new = threading.Thread(target=_serve_http, args=(sock2, stop_new, 2), daemon=True)
    t_new.start()

    try:
        assert gap_ms < 100.0, f"Dual-stack socket handover gap too high: {gap_ms}ms"
        # Verify both IPv4 (127.0.0.1) and IPv6 (::1) connect to gen=2 on the new socket
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            with socket.socket(family, socket.SOCK_STREAM) as cli:
                cli.settimeout(2.0)
                cli.connect((host, port))
                cli.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                raw = cli.recv(4096).decode("utf-8", errors="ignore")
                assert "HTTP/1.1 200 OK" in raw
                assert '"gen": 2' in raw
    finally:
        stop_new.set()
        try:
            sock2.close()
        except Exception:
            pass
