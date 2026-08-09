"""
Minimal pytest tests for app/utils/model_resolver.py
"""
import pytest
from app.utils.model_resolver import (
    resolve_model_conn,
    resolve_active_connection,
    _choose_connection_for_model,
)


# ─── Shared fixtures ─────────────────────────────────────────────────────────

def _make_provider(connections, models):
    """Helper: build a minimal provider config dict."""
    return {"connections": connections, "models": models}


def _make_config(providers, combos=None, aliases=None):
    cfg = {"providers": providers}
    if combos is not None:
        cfg["combos"] = combos
    if aliases is not None:
        cfg["aliases"] = aliases
    return cfg


# ─── Test 1: connection_indexes selects correct connection ────────────────────

def test_connection_indexes_picks_second_connection():
    """
    When a model has connection_indexes: [1], only the second connection
    (index 1) must be chosen even when both connections are enabled.
    Random is deterministic by asserting we always get the key of index 1.
    """
    conn0 = {"api_key": "key-0", "enabled": True}
    conn1 = {"api_key": "key-1", "enabled": True}
    prov = _make_provider(
        connections=[conn0, conn1],
        models=[{"id": "my-model", "enabled": True, "connection_indexes": [1]}],
    )
    config = _make_config({"prov-a": prov})

    results = set()
    for _ in range(30):  # run many times to confirm determinism
        conn, model = resolve_model_conn(config, "my-model")
        assert conn is not None, "Expected a connection to be returned"
        results.add(conn["api_key"])

    assert results == {"key-1"}, (
        f"Expected only key-1 to be selected, but got: {results}"
    )


# ─── Test 2: combo fallback when connection_indexes has no enabled match ──────

def test_combo_fallback_when_indexed_connection_disabled():
    """
    Combo fallback: first chain entry has connection_indexes=[1] but
    connection at index 1 is disabled → _choose_connection_for_model
    returns None → resolver must fall through to the second chain entry.
    """
    # Provider A: two connections; index 1 disabled
    conn_a0 = {"api_key": "key-a0", "enabled": True}
    conn_a1 = {"api_key": "key-a1", "enabled": False}  # disabled
    prov_a = _make_provider(
        connections=[conn_a0, conn_a1],
        models=[{"id": "model-x", "enabled": True, "connection_indexes": [1]}],
    )

    # Provider B: single enabled connection — this should be chosen via fallback
    conn_b0 = {"api_key": "key-b0", "enabled": True}
    prov_b = _make_provider(
        connections=[conn_b0],
        models=[{"id": "model-y", "enabled": True}],
    )

    config = _make_config(
        providers={"prov-a": prov_a, "prov-b": prov_b},
        combos=[
            {
                "alias": "my-combo",
                "chain": [
                    {"provider": "prov-a", "model": "model-x"},
                    {"provider": "prov-b", "model": "model-y"},
                ],
            }
        ],
    )

    conn, model = resolve_model_conn(config, "my-combo")
    assert conn is not None, "Expected fallback to second chain entry"
    assert conn["api_key"] == "key-b0", (
        f"Expected fallback key-b0, got {conn['api_key']!r}"
    )
    assert model == "model-y"


# ─── Test 3: missing indexes → legacy random among enabled connections ────────

def test_missing_indexes_uses_legacy_random_enabled():
    """
    When a model has no connection_indexes (or metadata is absent),
    the resolver must pick randomly from ALL enabled connections and
    must never return a disabled connection's key.
    """
    conn_enabled_0 = {"api_key": "enabled-0", "enabled": True}
    conn_disabled  = {"api_key": "disabled-1", "enabled": False}
    conn_enabled_1 = {"api_key": "enabled-2", "enabled": True}

    prov = _make_provider(
        connections=[conn_enabled_0, conn_disabled, conn_enabled_1],
        models=[{"id": "bare-model", "enabled": True}],  # no connection_indexes
    )
    config = _make_config({"prov-c": prov})

    seen = set()
    for _ in range(60):
        conn, _ = resolve_model_conn(config, "bare-model")
        assert conn is not None
        seen.add(conn["api_key"])

    # Disabled key must never appear
    assert "disabled-1" not in seen, "Disabled connection was returned"
    # At least one of the two enabled keys must appear
    assert seen.issubset({"enabled-0", "enabled-2"}), (
        f"Unexpected keys in results: {seen}"
    )


# ─── Test 4: resolve_active_connection returns original index ────────────────

def test_resolve_active_connection_returns_index():
    """resolve_active_connection must return the original connection index."""
    config = {
        "providers": {
            "prov-a": {
                "connections": [
                    {"api_key": "key0", "base_url": "http://0", "enabled": True},
                    {"api_key": "key1", "base_url": "http://1", "enabled": True},
                ],
                "models": [{"id": "model-x", "enabled": True, "connection_indexes": [1]}],
            }
        }
    }
    conn, idx = resolve_active_connection(config, "prov-a", "model-x")
    assert conn is not None
    assert idx == 1
    assert conn["api_key"] == "key1"


def test_resolve_active_connection_no_enabled_returns_none():
    """All connections disabled -> (None, None)."""
    config = {
        "providers": {
            "prov-a": {
                "connections": [{"api_key": "k", "enabled": False}],
                "models": [{"id": "m", "enabled": True}],
            }
        }
    }
    conn, idx = resolve_active_connection(config, "prov-a", "m")
    assert conn is None
    assert idx is None


class _FakeBreaker:
    """Minimal breaker stub: enabled + filter_healthy_connections removing index 0."""

    enabled = True

    def __init__(self, removed_indices):
        self._removed = set(removed_indices)

    def filter_healthy_connections(self, provider, model, connections):
        return [e for e in connections if e["index"] not in self._removed]


def test_resolve_active_connection_with_breaker():
    """Circuit breaker filters out OPEN connections."""
    config = {
        "providers": {
            "prov-a": {
                "connections": [
                    {"api_key": "k0", "enabled": True},
                    {"api_key": "k1", "enabled": True},
                ],
                "models": [{"id": "m", "enabled": True}],
            }
        }
    }
    breaker = _FakeBreaker(removed_indices=[0])
    conn, idx = resolve_active_connection(config, "prov-a", "m", breaker=breaker)
    assert conn is not None
    assert idx == 1
    assert conn["api_key"] == "k1"


def test_resolve_active_connection_no_breaker():
    """Without breaker, all enabled connections are eligible."""
    config = {
        "providers": {
            "prov-a": {
                "connections": [
                    {"api_key": "k0", "enabled": True},
                    {"api_key": "k1", "enabled": True},
                ],
                "models": [{"id": "m", "enabled": True}],
            }
        }
    }
    conn, idx = resolve_active_connection(config, "prov-a", "m", breaker=None)
    assert conn is not None
    assert idx in (0, 1)


def test_choose_connection_for_model_with_breaker_param():
    """The low-level function also accepts breaker (backward compatible)."""
    config = {
        "providers": {
            "prov-a": {
                "connections": [{"api_key": "k", "enabled": True}],
                "models": [{"id": "m", "enabled": True}],
            }
        }
    }
    # Without breaker - must still work (backward compat)
    conn = _choose_connection_for_model(config["providers"]["prov-a"], "m")
    assert conn is not None
