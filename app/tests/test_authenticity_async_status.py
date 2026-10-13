"""Regression: async authenticity-test job status contract (2026-10-12).

POST /api/authenticity/test is asynchronous: it returns 202 and a background
task owns the verdict; GET /api/authenticity/test/status/{model_id} is the
only way a consumer learns the terminal state. Two write-side contracts
protect that poller and are pinned here:

1. The cancel endpoint must write a COMPLETE terminal status
   (status=cancelled + verdict payload). A keyless {step, label, ts} dict
   reads as 'running' forever to any consumer checking the `status` key.
2. The job's terminal write must honour a cancel flag that lands AFTER the
   escalation decision -- a cancelled job must never report 'done' over the
   cancel status (the race the cancel-flag guard in _job closes).

Endpoint functions are invoked directly (repo pattern: asyncio.run, no
pytest-asyncio); fakes replace _auth_check / persistence / config so no
probe traffic or config writes occur. conftest's BSL_CONFIG_PATH isolation
covers the app.main import (Rule 7).
"""
import asyncio
import json
from types import SimpleNamespace

from app import main


def _cleanup(model_id):
    main._AUTH_TEST_STATUS.pop(model_id, None)
    main._AUTH_TEST_CANCEL.discard(model_id)
    main._AUTH_TEST_TASKS.pop(model_id, None)


class _FakeVerdict:
    """Minimal stand-in: strong match so auto-escalation is skipped."""
    verdict = "real"
    submodel_score = 0.95
    submodel_match = "fake/strong"
    evidence = {}

    def to_dict(self):
        return {"verdict": "real", "headline": "REAL", "confidence": 0.95}


def _patch_gate_fakes(monkeypatch, model_id, cancel_midrun):
    async def _fake_auth_check(**_kw):
        if cancel_midrun:
            # Simulate the user cancel landing after the escalation decision
            # but before the job's terminal write.
            main._AUTH_TEST_CANCEL.add(model_id)
        return _FakeVerdict()

    monkeypatch.setattr(main, "_auth_check", _fake_auth_check)
    monkeypatch.setattr(main, "_auth_persist_verdict", lambda *a, **k: None)
    monkeypatch.setattr(main, "_router_self_endpoint_and_key",
                        lambda: ("http://127.0.0.1:6969", "x"))
    monkeypatch.setattr(main, "_auth_cfg_get", lambda: {})


def _fake_request(payload):
    async def _json():
        return payload
    return SimpleNamespace(json=_json)


# ---- cancel endpoint ---------------------------------------------------------

def test_cancel_endpoint_sets_flag_and_complete_terminal_status():
    """The cancel status MUST carry `status` + `verdict` keys -- pollers treat
    a missing status as 'running' (legacy compat) and would spin forever.
    The cooperative cancel flag is what actually unwinds the running gate at
    the next on_progress boundary."""
    mid = "test-model-cancel-contract"
    _cleanup(mid)
    try:
        resp = asyncio.run(main.auth_test_cancel(mid))
        assert json.loads(resp.body)["cancelled"] is True
        assert mid in main._AUTH_TEST_CANCEL
        st = main._AUTH_TEST_STATUS[mid]
        assert st["step"] == "cancelled"
        assert st["status"] == "cancelled"
        assert st["verdict"]["verdict"] == "cancelled"
        assert st["verdict"]["headline"] == "Test cancelled by user"
        assert "ts" in st
    finally:
        _cleanup(mid)


# ---- job terminal-write race -------------------------------------------------

def test_done_write_honours_cancel_flag_set_midrun(monkeypatch):
    """Cancel lands between the escalation guard and the terminal write:
    the job must report 'cancelled', never clobber it with 'done'."""
    mid = "test-model-done-race"
    _cleanup(mid)
    _patch_gate_fakes(monkeypatch, mid, cancel_midrun=True)

    async def _drive():
        resp = await main.run_auth_test(_fake_request({"model_id": mid}))
        assert resp.status_code == 202
        task = main._AUTH_TEST_TASKS[mid]
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)

    try:
        asyncio.run(_drive())
        st = main._AUTH_TEST_STATUS[mid]
        assert st["status"] == "cancelled"
        assert st["step"] == "cancelled"
        assert st["verdict"]["verdict"] == "cancelled"
        # finally-block hygiene: the flag is consumed, not left armed to veto
        # the next run for this model.
        assert mid not in main._AUTH_TEST_CANCEL
    finally:
        _cleanup(mid)


def test_done_write_reports_done_without_cancel(monkeypatch):
    """Happy path: no cancel flag -> terminal status 'done' carrying the
    verdict dict for the poller to render."""
    mid = "test-model-done-happy"
    _cleanup(mid)
    _patch_gate_fakes(monkeypatch, mid, cancel_midrun=False)

    async def _drive():
        resp = await main.run_auth_test(_fake_request({"model_id": mid}))
        assert resp.status_code == 202
        task = main._AUTH_TEST_TASKS[mid]
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)

    try:
        asyncio.run(_drive())
        st = main._AUTH_TEST_STATUS[mid]
        assert st["status"] == "done"
        assert st["step"] == "done"
        assert st["verdict"]["verdict"] == "real"
    finally:
        _cleanup(mid)


# ---- status endpoint default -------------------------------------------------

def test_status_endpoint_returns_idle_for_unknown_model():
    """Unknown model -> complete idle shape (the `status` key must be present
    so consumers never fall back to the legacy 'running' assumption)."""
    resp = asyncio.run(main.auth_test_status("no-such-model-xyz"))
    payload = json.loads(resp.body)
    assert payload["status"] == "idle"
    assert payload["step"] == "idle"
