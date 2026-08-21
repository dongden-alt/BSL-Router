"""
Minimal pytest tests for app/utils/model_resolver.py
"""
import pytest
from app.utils.model_resolver import (
    resolve_model_conn,
    resolve_active_connection,
    _choose_connection_for_model,
    reset_provider_round_robin_state,
    _PROVIDER_RR_STATE,
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


# ─── Test 3: missing indexes → top-first determinism among enabled connections ─

def test_missing_indexes_uses_top_first_deterministic():
    """
    When a model has no connection_indexes (or metadata is absent),
    the resolver must pick the LOWEST enabled index deterministically
    (top-first mode) and must never return a disabled connection's key.
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

    # Top-first: always the lowest enabled index (enabled-0)
    assert seen == {"enabled-0"}, (
        f"Expected only enabled-0 (top-first), but got: {seen}"
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


# ─── Multi-key selection mode tests ──────────────────────────────────────────


def _provider_with_keys(keys, round_robin=False, model_id="m"):
    """Build a provider whose connections carry api_key names from `keys`."""
    connections = [{"api_key": k, "enabled": True} for k in keys]
    return {
        "connections": connections,
        "models": [{"id": model_id, "enabled": True}],
        "round_robin": round_robin,
    }


def test_top_first_without_round_robin():
    """round_robin falsy (default): 2 enabled keys -> always index 0."""
    reset_provider_round_robin_state()
    prov = _provider_with_keys(["k0", "k1"], round_robin=False)
    config = _make_config({"prov-a": prov})

    for _ in range(50):
        conn, idx = resolve_active_connection(config, "prov-a", "m")
        assert conn["api_key"] == "k0"
        assert idx == 0


def test_top_first_skips_disabled_top():
    """round_robin falsy: keys [disabled, enabled] -> always index 1."""
    reset_provider_round_robin_state()
    connections = [
        {"api_key": "k0", "enabled": False},
        {"api_key": "k1", "enabled": True},
    ]
    prov = {
        "connections": connections,
        "models": [{"id": "m", "enabled": True}],
        "round_robin": False,
    }
    config = _make_config({"prov-a": prov})

    for _ in range(50):
        conn, idx = resolve_active_connection(config, "prov-a", "m")
        assert conn["api_key"] == "k1"
        assert idx == 1


def test_round_robin_rotates():
    """round_robin true, 2 enabled keys -> alternating indices over even calls."""
    reset_provider_round_robin_state()
    prov = _provider_with_keys(["k0", "k1"], round_robin=True)
    config = _make_config({"prov-a": prov})

    indices = []
    for _ in range(20):
        conn, idx = resolve_active_connection(config, "prov-a", "m")
        indices.append(idx)

    expected = [i % 2 for i in range(20)]
    assert indices == expected, f"Expected alternating {expected}, got {indices}"


def test_round_robin_with_breaker():
    """round_robin true, breaker removes index 0 -> RR rotates only over remaining."""
    reset_provider_round_robin_state()
    prov = _provider_with_keys(["k0", "k1", "k2"], round_robin=True)
    config = _make_config({"prov-a": prov})
    breaker = _FakeBreaker(removed_indices=[0])

    indices = []
    for _ in range(12):
        conn, idx = resolve_active_connection(config, "prov-a", "m", breaker=breaker)
        indices.append(idx)

    # Pool is [k1(idx1), k2(idx2)]; rotation yields 1,2,1,2,...
    expected = [1, 2] * 6
    assert indices == expected, f"Expected {expected}, got {indices}"


def test_breaker_top_first_fallback():
    """Top key OPEN via breaker -> picks next; when breaker clears -> back to top."""
    reset_provider_round_robin_state()
    prov = _provider_with_keys(["k0", "k1"], round_robin=False)
    config = _make_config({"prov-a": prov})

    # Index 0 is OPEN -> top-first falls through to index 1
    breaker_open = _FakeBreaker(removed_indices=[0])
    conn, idx = resolve_active_connection(config, "prov-a", "m", breaker=breaker_open)
    assert conn["api_key"] == "k1"
    assert idx == 1

    # Breaker clears (no removals) -> top-first resumes index 0
    breaker_clear = _FakeBreaker(removed_indices=[])
    conn, idx = resolve_active_connection(config, "prov-a", "m", breaker=breaker_clear)
    assert conn["api_key"] == "k0"
    assert idx == 0


def test_reset_provider_round_robin_state_clears_counters():
    """reset_provider_round_robin_state() wipes the module-level RR dict."""
    reset_provider_round_robin_state()
    prov = _provider_with_keys(["k0", "k1"], round_robin=True)
    config = _make_config({"prov-a": prov})

    # First pick -> index 0
    _, idx = resolve_active_connection(config, "prov-a", "m")
    assert idx == 0
    # Second pick -> index 1
    _, idx = resolve_active_connection(config, "prov-a", "m")
    assert idx == 1

    reset_provider_round_robin_state()
    # After reset, rotation restarts at index 0
    _, idx = resolve_active_connection(config, "prov-a", "m")
    assert idx == 0
