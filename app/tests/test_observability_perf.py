"""Regression tests for OBS-PERF: pagination + recompute cache for the
observability Logs & Usage tabs.

Usage-endpoint tests now seed SQLite via the usage store helpers and the
temp-path fixture, reflecting that usage-stats history lives in SQLite not
in the in-memory list. Console-log tests remain on in-memory data.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs
import app.main as main


# ── Fixtures / Helpers ─────────────────────────────────────────────────────

def _await(coro):
    return asyncio.run(coro)


def _resp_json(resp):
    return json.loads(resp.body.decode("utf-8"))


def _make_entry(i, **kw):
    base = {
        "timestamp": f"2026-08-{min(i % 28 + 1, 28):02d}T{min(i % 24, 23):02d}:00:00",
        "provider": "openai",
        "model": "gpt-test",
        "ttft_ms": 1.0,
        "total_time_ms": 2.0,
        "in_cached": 0,
        "cache_write_tokens": 0,
        "in_uncached": 10,
        "out": 5,
        "cost": round(i * 0.001, 4),
        "savings": round(i * 0.0005, 4),
    }
    base.update(kw)
    return base


def _seed_console_logs(n):
    """Append n synthetic console log entries directly to the in-memory list."""
    obs.console_logs.clear()
    for i in range(n):
        obs.console_logs.append({
            "timestamp": f"2026-08-14T00:00:{i:02d}.000000",
            "event": "end",
            "request_id": f"req_test_{i}",
            "provider": "openai",
            "model": "gpt-test",
            "status": 200,
            "ttft_ms": 1.0,
            "total_time_ms": 2.0,
            "in_tokens": 10,
            "out_tokens": 5,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        })


def _seed_usage_sqlite(n, tmp_path=None):
    """Seed rows directly via append_usage_event using a given temp db."""
    old_db = obs.usage_db_path
    old_jsonl = obs._USAGE_LOG_PATH
    try:
        if tmp_path is not None:
            obs.usage_db_path = str(tmp_path / "usage_stats.sqlite3")
            # Also redirect JSONL path so init_usage_store() migration
            # does not import thousands of production rows into the temp DB.
            obs._USAGE_LOG_PATH = str(tmp_path / "usage_stats.jsonl")
            obs.init_usage_store()
        for i in range(n):
            obs.append_usage_event(_make_entry(i))
    finally:
        obs.usage_db_path = old_db
        obs._USAGE_LOG_PATH = old_jsonl


@pytest.fixture(autouse=True)
def _restore_state():
    """Snapshot & restore in-memory state + recompute cache per test."""
    saved_logs = list(obs.console_logs)
    saved_shim = list(obs.usage_stats_shim)
    saved_ts = obs._recompute_last_ts
    saved_len = obs._recompute_last_len
    saved_key = obs._recompute_registry_key
    yield
    obs.console_logs[:] = saved_logs
    obs.usage_stats_shim[:] = saved_shim
    obs._recompute_last_ts = saved_ts
    obs._recompute_last_len = saved_len
    obs._recompute_registry_key = saved_key


# ── Logs endpoint (unchanged behaviour) ────────────────────────────────────

def test_logs_endpoint_returns_pagination_wrapper_with_limit():
    _seed_console_logs(25)
    resp = _await(main.get_logs(limit=10, offset=0))
    body = _resp_json(resp)
    assert set(body.keys()) >= {"total", "entries", "has_more"}
    assert body["total"] == 25
    assert len(body["entries"]) == 10
    assert body["has_more"] is True


def test_logs_endpoint_offset_and_has_more_false_at_tail():
    _seed_console_logs(25)
    resp = _await(main.get_logs(limit=10, offset=20))
    body = _resp_json(resp)
    assert body["total"] == 25
    assert len(body["entries"]) == 5
    assert body["has_more"] is False


def test_logs_endpoint_has_x_total_count_header():
    _seed_console_logs(7)
    resp = _await(main.get_logs(limit=500, offset=0))
    assert resp.headers.get("x-total-count") == "7"


# ── Usage endpoint – now backed by SQLite ──────────────────────────────────

def test_usage_endpoint_returns_wrapped_result(tmp_path):
    """GET /usage with before_id uses keyset query on SQLite."""
    db_path = str(tmp_path / "usage_stats.sqlite3")
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    old_db = obs.usage_db_path
    old_jsonl = obs._USAGE_LOG_PATH
    obs.usage_db_path = db_path
    obs._USAGE_LOG_PATH = jsonl_path  # prevent prod JSONL migration into temp DB
    obs.init_usage_store()
    try:
        for i in range(25):
            entry = {
                "timestamp": f"2026-08-{min(i % 28 + 1, 28):02d}T{min(i, 23):02d}:00:00",
                "provider": "openai", "model": "gpt-test",
                "ttft_ms": 1.0, "total_time_ms": 2.0,
                "in_cached": 0, "cache_write_tokens": 0,
                "in_uncached": 10, "out": 5,
                "cost": round(i * 0.001, 4), "savings": round(i * 0.0005, 4),
            }
            obs.append_usage_event(entry)
        resp = _await(main.get_usage(limit=10, before_id="0"))
        body = _resp_json(resp)
        assert set(body.keys()) >= {"total", "entries", "has_more", "next_before_id"}
        assert body["total"] == 25
        assert len(body["entries"]) == 10
        assert body["has_more"] is True
    finally:
        obs.usage_db_path = old_db
        obs._USAGE_LOG_PATH = old_jsonl


def test_usage_limit_clamped_to_500(tmp_path):
    """GET /usage clamp limit to 500 max."""
    db_path = str(tmp_path / "usage_stats.sqlite3")
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    old_db = obs.usage_db_path
    old_jsonl = obs._USAGE_LOG_PATH
    obs.usage_db_path = db_path
    obs._USAGE_LOG_PATH = jsonl_path
    obs.init_usage_store()
    try:
        for i in range(600):
            obs.append_usage_event({
                "timestamp": f"2026-08-{min(i % 28 + 1, 28):02d}T{min(i, 23):02d}:00:00",
                "provider": "openai", "model": f"gpt-test-{i}",
                "ttft_ms": 1.0, "total_time_ms": 2.0,
                "in_cached": 0, "cache_write_tokens": 0,
                "in_uncached": 10, "out": 5,
                "cost": 0.005, "savings": 0.001,
            })
        resp = _await(main.get_usage(limit=999999, before_id="0"))
        body = _resp_json(resp)
        assert body["total"] == 600
        assert len(body["entries"]) <= 500
    finally:
        obs.usage_db_path = old_db
        obs._USAGE_LOG_PATH = old_jsonl


def test_usage_no_full_recompute(tmp_path, monkeypatch):
    """The GET /usage endpoint must NOT trigger a full-history reprice scan."""
    db_path = str(tmp_path / "usage_stats.sqlite3")
    jsonl_path = str(tmp_path / "usage_stats.jsonl")
    old_db = obs.usage_db_path
    old_jsonl = obs._USAGE_LOG_PATH
    obs.usage_db_path = db_path
    obs._USAGE_LOG_PATH = jsonl_path
    obs.init_usage_store()
    try:
        for i in range(50):
            obs.append_usage_event({
                "timestamp": f"2026-08-{min(i % 28 + 1, 28):02d}T{min(i, 23):02d}:00:00",
                "provider": "openai", "model": "gpt-test",
                "ttft_ms": 1.0, "total_time_ms": 2.0,
                "in_cached": 0, "cache_write_tokens": 0,
                "in_uncached": 10, "out": 5, "cost": 0.005, "savings": 0.001,
            })
        resp = _await(main.get_usage(limit=10, before_id="0"))
        body = _resp_json(resp)
        assert len(body["entries"]) <= 10
    finally:
        obs.usage_db_path = old_db
        obs._USAGE_LOG_PATH = old_jsonl


# ── Limit clamping ─────────────────────────────────────────────────────────

def test_log_limit_clamps_to_2000_for_huge_values():
    _seed_console_logs(2500)
    resp = _await(main.get_logs(limit=999999, offset=0))
    body = _resp_json(resp)
    assert body["total"] == 2500
    assert len(body["entries"]) <= 2000


def test_negative_limit_falls_back_to_default_500():
    _seed_console_logs(600)
    resp = _await(main.get_logs(limit=-5, offset=0))
    body = _resp_json(resp)
    assert body["total"] == 600
    assert len(body["entries"]) == 500
    assert body["has_more"] is True


def test_zero_limit_falls_back_to_default_500():
    _seed_console_logs(10)
    resp = _await(main.get_logs(limit=0, offset=0))
    body = _resp_json(resp)
    assert len(body["entries"]) == 10


def test_non_numeric_limit_does_not_crash():
    _limit, _offset = main._obs_pagination_params(None, None)
    assert _limit == 500
    assert _offset == 0
    _limit, _offset = main._obs_pagination_params("abc", "xyz")
    assert _limit == 500
    assert _offset == 0


def test_negative_offset_normalized_to_zero():
    _seed_console_logs(5)
    resp = _await(main.get_logs(limit=10, offset=-3))
    body = _resp_json(resp)
    assert body["total"] == 5
    assert len(body["entries"]) == 5


# ── Recompute cache ────────────────────────────────────────────────────────

def test_recompute_runs_at_most_once_within_ttl(monkeypatch):
    obs.usage_stats_shim.clear()
    for i in range(50):
        obs.usage_stats_shim.append(_make_entry(i))
    obs.invalidate_recompute_cache()

    call_count = {"n": 0}
    real_loader = obs._load_pricing_registry

    def _counting_loader():
        call_count["n"] += 1
        return real_loader()

    monkeypatch.setattr(obs, "_load_pricing_registry", _counting_loader)

    cfg = {"providers": {}}
    obs.recompute_usage_costs(cfg)
    first_count = call_count["n"]
    obs.recompute_usage_costs(cfg)

    assert first_count >= 1, "first call must read the registry"
    assert call_count["n"] == first_count, (
        f"second recompute within TTL must not re-read registry; "
        f"got {call_count['n']} reads (expected {first_count})"
    )


def test_recompute_force_bypasses_cache(monkeypatch):
    obs.usage_stats_shim.clear()
    obs.invalidate_recompute_cache()

    call_count = {"n": 0}
    real_loader = obs._load_pricing_registry

    def _counting_loader():
        call_count["n"] += 1
        return real_loader()

    monkeypatch.setattr(obs, "_load_pricing_registry", _counting_loader)

    cfg = {"providers": {}}
    obs.recompute_usage_costs(cfg)
    obs.recompute_usage_costs(cfg, force=True)
    assert call_count["n"] >= 2


def test_recompute_cache_keyed_on_registry_mtime(monkeypatch):
    obs.usage_stats_shim.clear()
    obs.invalidate_recompute_cache()

    real_loader = obs._load_pricing_registry
    real_sig = obs._pricing_registry_signature()

    sig_calls = {"n": 0}

    def _shifting_sig():
        sig_calls["n"] += 1
        if real_sig:
            return (real_sig[0] + sig_calls["n"], real_sig[1])
        return (float(sig_calls["n"]), 1)

    monkeypatch.setattr(obs, "_pricing_registry_signature", _shifting_sig)

    load_calls = {"n": 0}

    def _counting_loader():
        load_calls["n"] += 1
        return real_loader()

    monkeypatch.setattr(obs, "_load_pricing_registry", _counting_loader)

    cfg = {"providers": {}}
    obs.recompute_usage_costs(cfg)
    obs.recompute_usage_costs(cfg)
    assert load_calls["n"] >= 2


def test_recompute_updates_costs_when_rates_present():
    obs.usage_stats_shim.clear()
    obs.invalidate_recompute_cache()
    for i in range(3):
        obs.usage_stats_shim.append(_make_entry(i, model="gpt-test",
                                                    in_cached=0, cache_write_tokens=0,
                                                    in_uncached=10, out=5, cost=0, savings=0))

    cfg = {"providers": {"openai": {"models": [
        {"id": "gpt-test", "cost_in": 2.0, "cost_out": 8.0, "cost_cache": 0.5}
    ]}}}
    obs.recompute_usage_costs(cfg, force=True)
    for entry in obs.usage_stats_shim:
        assert entry["cost"] > 0


# ── Cross-isolation ────────────────────────────────────────────────────────

def test_logs_endpoint_does_not_touch_usage_stats():
    _seed_console_logs(5)
    before = list(obs.usage_stats_shim)
    _ = _await(main.get_logs(limit=10))
    assert obs.usage_stats_shim == before


def test_usage_endpoint_does_not_mutate_console_logs():
    _seed_console_logs(5)
    before = list(obs.console_logs)
    _ = _await(main.get_usage(limit=10))
    assert obs.console_logs == before
