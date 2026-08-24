"""Integration tests for the OAuth quota merge path in /api/quota/status.

Closes audit finding F1: the 8 capture-unit tests isolate _capture_rate_limit_headers
but never exercise get_quota_status_endpoint's merge branch, which swaps a null probe
row for a passive registry entry when the connection is OAuth (refresh_token / access_token).

These tests stub cs_get_config (zero network) and seed _OAUTH_QUOTA, then drive the
real async endpoint and assert the merged shape: {remaining_pct, metric, source:"oauth_live", updated_at}.
"""
import asyncio
import json

import pytest


def _import_mod():
    import app.main as mod
    return mod


@pytest.fixture
def quota_state():
    """Isolate the two module-level globals the endpoint reads."""
    mod = _import_mod()
    mod._OAUTH_QUOTA.clear()
    mod._QUOTA_CACHE["providers"] = {}
    mod._QUOTA_CACHE["ts"] = 0  # force recompute (TTL-based cache is "stale")
    yield mod
    mod._OAUTH_QUOTA.clear()
    mod._QUOTA_CACHE["providers"] = {}
    mod._QUOTA_CACHE["ts"] = 0


def _oauth_cfg(connections):
    return {"providers": {"claude": {"base_url": "https://api.anthropic.com", "connections": connections}}}


def test_oauth_merge_full(monkeypatch, quota_state):
    """Both OAuth connections have captured registry entries -> both rows merged."""
    mod = quota_state
    cfg = _oauth_cfg([
        {"name": "c0", "access_token": "tok0"},
        {"name": "c1", "refresh_token": "rt1"},
    ])
    monkeypatch.setattr(mod, "cs_get_config", lambda: cfg)
    mod._OAUTH_QUOTA["claude"] = {
        0: {"remaining_pct": 73.5, "metric": "tokens", "window_reset": None, "updated_at": 1234.0},
        1: {"remaining_pct": 12.0, "metric": "requests", "window_reset": None, "updated_at": 5678.0},
    }
    resp = asyncio.run(mod.get_quota_status_endpoint(provider="claude"))
    data = json.loads(resp.body)
    assert data["cached"] is False  # forced recompute, not a stale serve
    row = data["providers"]["claude"]
    assert isinstance(row, list) and len(row) == 2
    assert row[0]["source"] == "oauth_live"
    assert row[0]["remaining_pct"] == 73.5
    assert row[0]["metric"] == "tokens"
    assert row[1]["source"] == "oauth_live"
    assert row[1]["remaining_pct"] == 12.0
    assert row[1]["metric"] == "requests"


def test_oauth_partial_registry(monkeypatch, quota_state):
    """Only one of two OAuth connections has traffic -> only that row merged, other stays null."""
    mod = quota_state
    cfg = _oauth_cfg([
        {"name": "c0", "access_token": "tok0"},
        {"name": "c1", "refresh_token": "rt1"},
    ])
    monkeypatch.setattr(mod, "cs_get_config", lambda: cfg)
    mod._OAUTH_QUOTA["claude"] = {0: {"remaining_pct": 50.0, "metric": "tokens", "updated_at": 1.0}}
    resp = asyncio.run(mod.get_quota_status_endpoint(provider="claude"))
    row = json.loads(resp.body)["providers"]["claude"]
    assert row[0]["source"] == "oauth_live" and row[0]["remaining_pct"] == 50.0
    assert row[1] is None  # cold connection, no registry hit


def test_oauth_cold_start_null(monkeypatch, quota_state):
    """OAuth connection but empty registry -> null row (frontend shows '∤ no data')."""
    mod = quota_state
    cfg = _oauth_cfg([{"name": "c0", "access_token": "tok0"}])
    monkeypatch.setattr(mod, "cs_get_config", lambda: cfg)
    # _OAUTH_QUOTA intentionally empty for claude
    resp = asyncio.run(mod.get_quota_status_endpoint(provider="claude"))
    row = json.loads(resp.body)["providers"]["claude"]
    assert row == [None]


def test_oauth_index_guard_out_of_range(monkeypatch, quota_state):
    """Registry index beyond the connection list must be ignored, never crash/shift."""
    mod = quota_state
    cfg = _oauth_cfg([{"name": "c0", "access_token": "tok0"}])
    monkeypatch.setattr(mod, "cs_get_config", lambda: cfg)
    mod._OAUTH_QUOTA["claude"] = {5: {"remaining_pct": 99.0, "metric": "tokens", "updated_at": 1.0}}
    resp = asyncio.run(mod.get_quota_status_endpoint(provider="claude"))
    row = json.loads(resp.body)["providers"]["claude"]
    assert row == [None]  # stale/ghost index ignored, no IndexError, no misalignment


def test_oauth_all_providers_path(monkeypatch, quota_state):
    """?provider= omitted -> merged entry appears under its provider key in the full map."""
    mod = quota_state
    cfg = _oauth_cfg([{"name": "c0", "access_token": "tok0"}])
    monkeypatch.setattr(mod, "cs_get_config", lambda: cfg)
    mod._OAUTH_QUOTA["claude"] = {0: {"remaining_pct": 80.0, "metric": "tokens", "updated_at": 1.0}}
    resp = asyncio.run(mod.get_quota_status_endpoint())
    data = json.loads(resp.body)
    row = data["providers"]["claude"]
    assert isinstance(row, list) and row[0]["source"] == "oauth_live"
