"""Regression tests for OBS-PERF: pagination + recompute cache for the
observability Logs & Usage tabs.

These tests call the two GET endpoints directly (house style: import the
async route functions from app.main and await them, rather than spinning up
a full TestClient lifespan) and exercise the recompute cache in app.observability.
See .brain/cc_tasks/observability-perf.md for the spec.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs
import app.main as main


# ── Helpers ──────────────────────────────────────────────────────────────────

def _await(coro):
    """Run an async route function and return its JSONResponse."""
    return asyncio.run(coro)


def _resp_json(resp):
    return json.loads(resp.body.decode("utf-8"))


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


def _seed_usage_stats(n):
    """Append n synthetic usage entries directly to the in-memory list."""
    obs.usage_stats.clear()
    for i in range(n):
        obs.usage_stats.append({
            "timestamp": f"2026-08-14T00:00:{i:02d}.000000",
            "provider": "openai",
            "model": "gpt-test",
            "ttft_ms": 1.0,
            "total_time_ms": 2.0,
            "in_cached": 0,
            "cache_write_tokens": 0,
            "in_uncached": 10,
            "out": 5,
            "cost": 0.0,
            "savings": 0.0,
        })


@pytest.fixture(autouse=True)
def _restore_state():
    """Snapshot & restore the in-memory log lists + recompute cache per test."""
    saved_logs = list(obs.console_logs)
    saved_usage = list(obs.usage_stats)
    saved_ts = obs._recompute_last_ts
    saved_len = obs._recompute_last_len
    saved_key = obs._recompute_registry_key
    yield
    obs.console_logs[:] = saved_logs
    obs.usage_stats[:] = saved_usage
    obs._recompute_last_ts = saved_ts
    obs._recompute_last_len = saved_len
    obs._recompute_registry_key = saved_key


# ── Fix A: backend pagination ────────────────────────────────────────────────

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
    assert len(body["entries"]) == 5  # 25 - 20
    assert body["has_more"] is False


def test_logs_endpoint_has_x_total_count_header():
    _seed_console_logs(7)
    resp = _await(main.get_logs(limit=500, offset=0))
    assert resp.headers.get("x-total-count") == "7"


def test_usage_endpoint_returns_pagination_wrapper_with_limit():
    _seed_usage_stats(25)
    resp = _await(main.get_usage(limit=10, offset=0))
    body = _resp_json(resp)
    assert set(body.keys()) >= {"total", "entries", "has_more"}
    assert body["total"] == 25
    assert len(body["entries"]) == 10
    assert body["has_more"] is True


# ── Fix A: limit clamping ────────────────────────────────────────────────────

def test_limit_clamps_to_2000_for_huge_values():
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
    # default 500 applied (not -5, not crash)
    assert len(body["entries"]) == 500
    assert body["has_more"] is True


def test_zero_limit_falls_back_to_default_500():
    _seed_console_logs(10)
    resp = _await(main.get_logs(limit=0, offset=0))
    body = _resp_json(resp)
    assert len(body["entries"]) == 10  # less than default 500


def test_non_numeric_limit_does_not_crash():
    """Query params arrive typed; confirm the clamp path is defensive."""
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


# ── Fix B: recompute cache ───────────────────────────────────────────────────

def test_recompute_runs_at_most_once_within_ttl(monkeypatch):
    """Two recompute calls within the 60s TTL must hit the pricing registry
    loader at most once (the second is a cache hit)."""
    _seed_usage_stats(50)
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
    obs.recompute_usage_costs(cfg)  # should be a cache hit

    assert first_count >= 1, "first call must read the registry"
    assert call_count["n"] == first_count, (
        f"second recompute within TTL must not re-read registry; "
        f"got {call_count['n']} reads (expected {first_count})"
    )


def test_recompute_force_bypasses_cache(monkeypatch):
    _seed_usage_stats(10)
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
    """A change in the registry file signature must force a recompute even
    inside the TTL window."""
    _seed_usage_stats(10)
    obs.invalidate_recompute_cache()

    real_loader = obs._load_pricing_registry
    real_sig = obs._pricing_registry_signature()

    sig_calls = {"n": 0}

    def _shifting_sig():
        sig_calls["n"] += 1
        # Return a different signature each call so the cache key never matches.
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
    obs.recompute_usage_costs(cfg)  # signature changed -> must recompute
    assert load_calls["n"] >= 2


def test_recompute_updates_costs_when_rates_present():
    _seed_usage_stats(3)
    obs.invalidate_recompute_cache()

    cfg = {"providers": {"openai": {"models": [
        {"id": "gpt-test", "cost_in": 2.0, "cost_out": 8.0, "cost_cache": 0.5}
    ]}}}
    obs.recompute_usage_costs(cfg, force=True)
    # cost should now be non-zero (10 uncached in @2/1M + 5 out @8/1M)
    for entry in obs.usage_stats:
        assert entry["cost"] > 0


# ── Cross-isolation ──────────────────────────────────────────────────────────

def test_logs_endpoint_does_not_touch_usage_stats():
    _seed_console_logs(5)
    _seed_usage_stats(3)
    before = list(obs.usage_stats)
    _ = _await(main.get_logs(limit=10))
    assert obs.usage_stats == before


def test_usage_endpoint_does_not_mutate_console_logs():
    _seed_console_logs(5)
    _seed_usage_stats(3)
    before = list(obs.console_logs)
    _ = _await(main.get_usage(limit=10))
    assert obs.console_logs == before
