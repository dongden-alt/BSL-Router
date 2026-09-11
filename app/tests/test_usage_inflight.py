"""Part C — live in-flight usage visibility regression tests.

The usage ledger is SQLite, written frozen-at-write by log_request →
append_usage_event (cost computed at commit). In-flight streams only appear
in SQLite ~2s AFTER they finish. Part C adds a bounded, self-healing
in-memory registry keyed by request_id so the Usage tab's live strip can
show streams the instant they start (log_request_start) and reclaim the slot
once they commit (log_request).

These tests cover:
  - register/complete lifecycle (start adds; end removes)
  - bounded eviction past _INFLIGHT_MAX
  - self-healing stale-orchard pruning in inflight_snapshot()
  - newest-first ordering + the age_ms field
  - the /api/observability/usage/inflight endpoint returning the snapshot
  - fail-open behaviour on bad input
"""
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs
import app.main as main


def _await(coro):
    return asyncio.run(coro)


def _resp_json(resp):
    return json.loads(resp.body.decode("utf-8"))


_CFG = {"providers": {}}


@pytest.fixture(autouse=True)
def _restore_inflight():
    saved = list(obs._INFLIGHT_REQUESTS.items())
    yield
    obs._INFLIGHT_REQUESTS.clear()
    obs._INFLIGHT_REQUESTS.update(saved)


# ── Lifecycle: start registers, log_request completes ──────────────────────

def test_log_request_start_registers_inflight():
    rid = obs.log_request_start("openai", "gpt-test", _CFG, stream=True, client="cc")
    assert rid in obs._INFLIGHT_REQUESTS
    rec = obs._INFLIGHT_REQUESTS[rid]
    assert rec["provider"] == "openai"
    assert rec["model"] == "gpt-test"
    assert rec["stream"] is True
    assert rec["client"] == "cc"
    assert rec["_started_epoch"] > 0
    assert rec["started_at"]


def test_log_request_200_completes_inflight():
    rid = obs.log_request_start("openai", "gpt-test", _CFG, stream=True)
    assert rid in obs._INFLIGHT_REQUESTS
    obs.log_request(
        provider="openai", model="gpt-test", status=200,
        ttft=0.2, in_tokens=10, out_tokens=5, cached_tokens=0,
        config=_CFG, total_time=0.5, request_id=rid,
    )
    assert rid not in obs._INFLIGHT_REQUESTS


def test_log_request_non200_completes_inflight():
    """Non-200 path (err_entry) must also reclaim the inflight slot."""
    rid = obs.log_request_start("openai", "gpt-test", _CFG, stream=False)
    assert rid in obs._INFLIGHT_REQUESTS
    obs.log_request(
        provider="openai", model="gpt-test", status=500,
        ttft=0.0, in_tokens=0, out_tokens=0, cached_tokens=0,
        config=_CFG, error_msg="boom", total_time=0.1, request_id=rid,
    )
    assert rid not in obs._INFLIGHT_REQUESTS


# ── Snapshot: ordering, age, self-healing ──────────────────────────────────

def test_inflight_snapshot_newest_first():
    r1 = obs.log_request_start("openai", "gpt-1", _CFG)
    time.sleep(0.01)
    r2 = obs.log_request_start("openai", "gpt-2", _CFG)
    snap = obs.inflight_snapshot()
    assert len(snap) == 2
    assert snap[0]["request_id"] == r2  # newest first
    assert snap[1]["request_id"] == r1
    # age_ms must be present and non-negative
    assert snap[0]["age_ms"] >= 0
    assert snap[1]["age_ms"] >= snap[0]["age_ms"]


def test_inflight_snapshot_completes_prune_entries():
    rid = obs.log_request_start("openai", "gpt-test", _CFG)
    obs._complete_inflight(rid)
    assert obs.inflight_snapshot() == []


def test_inflight_snapshot_self_heals_stale():
    """Orphaned entries older than _INFLIGHT_STALE_S are pruned on snapshot."""
    rid = obs.log_request_start("openai", "gpt-test", _CFG)
    # Forge an ancient start epoch to simulate an orphaned stream that never
    # reached log_request (server restarted against a live request).
    obs._INFLIGHT_REQUESTS[rid]["_started_epoch"] = time.time() - (obs._INFLIGHT_STALE_S + 60)
    snap = obs.inflight_snapshot()
    assert snap == []
    assert rid not in obs._INFLIGHT_REQUESTS  # pruned, not just hidden


def test_inflight_snapshot_fresh_entry_survives():
    rid = obs.log_request_start("openai", "gpt-test", _CFG)
    snap = obs.inflight_snapshot()
    assert len(snap) == 1
    assert snap[0]["request_id"] == rid


# ── Bounding: eviction past _INFLIGHT_MAX ───────────────────────────────────

def test_inflight_registry_evicts_oldest_past_max(monkeypatch):
    monkeypatch.setattr(obs, "_INFLIGHT_MAX", 3)
    obs._INFLIGHT_REQUESTS.clear()
    ids = [obs.log_request_start("openai", f"gpt-{i}", _CFG) for i in range(5)]
    assert len(obs._INFLIGHT_REQUESTS) == 3
    # Oldest two evicted; newest three retained.
    assert ids[0] not in obs._INFLIGHT_REQUESTS
    assert ids[1] not in obs._INFLIGHT_REQUESTS
    assert ids[4] in obs._INFLIGHT_REQUESTS


# ── Fail-open ───────────────────────────────────────────────────────────────

def test_register_inflight_ignores_empty_request_id():
    n = len(obs._INFLIGHT_REQUESTS)
    obs._register_inflight("", {"provider": "x"})
    assert len(obs._INFLIGHT_REQUESTS) == n


def test_complete_inflight_missing_is_noop():
    n = len(obs._INFLIGHT_REQUESTS)
    obs._complete_inflight("does-not-exist")
    obs._complete_inflight(None)
    assert len(obs._INFLIGHT_REQUESTS) == n


# ── Endpoint: /api/observability/usage/inflight ─────────────────────────────

def test_inflight_endpoint_returns_snapshot_shape():
    rid = obs.log_request_start("openai", "gpt-test", _CFG, stream=True, client="cc")
    resp = _await(main.get_usage_inflight())
    body = _resp_json(resp)
    assert "inflight" in body
    lst = body["inflight"]
    assert isinstance(lst, list)
    assert len(lst) == 1
    row = lst[0]
    assert row["request_id"] == rid
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-test"
    assert row["stream"] is True
    assert row["client"] == "cc"
    assert "age_ms" in row and "started_at" in row


def test_inflight_endpoint_empty_when_no_active_streams():
    resp = _await(main.get_usage_inflight())
    body = _resp_json(resp)
    assert body == {"inflight": []}


def test_inflight_endpoint_does_not_mutate_registry():
    """The read endpoint must be side-effect-free on the registry (except
    self-healing stale pruning, which does not fire for fresh entries)."""
    rid = obs.log_request_start("openai", "gpt-test", _CFG)
    _ = _await(main.get_usage_inflight())
    assert rid in obs._INFLIGHT_REQUESTS  # fresh entry preserved
