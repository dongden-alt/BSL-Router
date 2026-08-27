"""Unit tests for the key-health feature set (2026-08-28 audit, G2).

Covers the dim / last_used / reset surface added in commit 0bf1e96 and the
G1 image/video hooks' underlying breaker contract:

  1. record_selection stamps last_used (fresh age_seconds, model recorded)
  2. rate_limit outcome dims the key (status_extras payload)
  3. success outcome un-dims (recovery via real call)
  4. reset_connection clears ONE key only (others untouched)
  5. reset_all clears every dim
  6. disabled breaker → record_outcome no-ops → dim never set (G5 semantics)
  7. disabled breaker → record_selection still stamps (display-only tracking)
  8. status_extras returns well-formed dimmed + last_used arrays

Pure unit tests on CircuitBreaker — no HTTP, no app import (pattern follows
test_key_failover.py).
"""

from __future__ import annotations

import time

from app.circuit_breaker import CircuitBreaker
from app.utils.model_resolver import resolve_active_connection


def _breaker(enabled: bool = True, threshold: int = 3, recovery: int = 30) -> CircuitBreaker:
    return CircuitBreaker({
        "circuit_breaker": {
            "enabled": enabled,
            "failure_threshold": threshold,
            "recovery_timeout": recovery,
        }
    })


P = "testprov"
M = "test-model"


# ── 1. record_selection ────────────────────────────────────────────────────

def test_record_selection_stamps_last_used():
    b = _breaker()
    t0 = time.time()
    b.record_selection(P, M, 1)
    extras = b.status_extras()
    assert len(extras["last_used"]) == 1
    entry = extras["last_used"][0]
    assert entry["provider"] == P
    assert entry["conn_index"] == 1
    assert entry["model"] == M
    # status_extras rounds ts to 1 decimal, so it can sit a fraction below
    # t0 — allow a 1s tolerance rather than asserting exact monotonicity.
    assert entry["ts"] >= t0 - 1.0
    assert 0.0 <= entry["age_seconds"] < 5.0


def test_record_selection_overwrites_same_key():
    b = _breaker()
    b.record_selection(P, "model-a", 0)
    time.sleep(0.01)
    b.record_selection(P, "model-b", 0)
    used = b.status_extras()["last_used"]
    assert len(used) == 1
    assert used[0]["model"] == "model-b"


def test_record_selection_failopen_on_garbage():
    b = _breaker()
    # int(None) raises inside the try — must swallow, not propagate.
    b.record_selection(P, M, None)
    assert b.status_extras()["last_used"] == []


# ── 2. dim on rate-limit ───────────────────────────────────────────────────

def test_rate_limit_dims_key():
    b = _breaker()
    b.record_outcome(P, M, 0, 429, "rate limited")
    dimmed = b.status_extras()["dimmed"]
    assert len(dimmed) == 1
    assert dimmed[0]["provider"] == P
    assert dimmed[0]["conn_index"] == 0
    assert dimmed[0]["age_seconds"] < 5.0


def test_auth_error_dims_and_opens_immediately():
    b = _breaker()
    b.record_outcome(P, M, 2, 401, "unauthorized")
    dimmed = b.status_extras()["dimmed"]
    assert len(dimmed) == 1
    # Auth is immediate-OPEN (below threshold 3).
    is_open, _ = b.is_open(P, M, 2)
    assert is_open is True


# ── 3. un-dim on success ───────────────────────────────────────────────────

def test_success_undims_key():
    b = _breaker()
    b.record_outcome(P, M, 0, 429, "rate limited")
    assert len(b.status_extras()["dimmed"]) == 1
    # The recovery call: a real request succeeds through this key.
    b.record_outcome(P, M, 0, 200, "")
    assert b.status_extras()["dimmed"] == []


def test_dim_survives_unrelated_key_activity():
    b = _breaker()
    b.record_outcome(P, M, 0, 429, "rate limited")
    # Success on a DIFFERENT key must not un-dim key 0.
    b.record_outcome(P, M, 1, 200, "")
    dimmed = b.status_extras()["dimmed"]
    assert len(dimmed) == 1
    assert dimmed[0]["conn_index"] == 0


# ── 4. reset_connection (admin toggle off→on) ─────────────────────────────

def test_reset_connection_clears_only_that_key():
    b = _breaker()
    b.record_outcome(P, "m1", 0, 429, "rate limited")
    b.record_outcome(P, "m2", 1, 429, "rate limited")
    b.record_selection(P, "m1", 0)
    b.record_selection(P, "m2", 1)

    b.reset_connection(P, 0)

    # Key 1 keeps its dim; key 0 is fully wiped.
    dimmed = b.status_extras()["dimmed"]
    assert len(dimmed) == 1
    assert dimmed[0]["conn_index"] == 1
    # last_used for key 0 wiped, key 1 kept.
    used = {u["conn_index"] for u in b.status_extras()["last_used"]}
    assert used == {1}
    # Breaker state for (P, *, 0) fully forgotten — next request is a probe.
    is_open, _ = b.is_open(P, "m1", 0)
    assert is_open is False


def test_reset_connection_tolerates_bad_index():
    b = _breaker()
    b.record_outcome(P, M, 0, 429, "rate limited")
    b.reset_connection(P, "not-a-number")  # must not raise
    assert len(b.status_extras()["dimmed"]) == 1


# ── 5. reset_all ────────────────────────────────────────────────────────────

def test_reset_all_clears_every_dim():
    b = _breaker()
    for idx in range(3):
        b.record_outcome(P, M, idx, 429, "rate limited")
    assert len(b.status_extras()["dimmed"]) == 3
    b.reset_all()
    assert b.status_extras()["dimmed"] == []


# ── 6/7. disabled-breaker semantics (G5) ───────────────────────────────────

def test_disabled_breaker_never_dims():
    b = _breaker(enabled=False)
    b.record_outcome(P, M, 0, 429, "rate limited")
    b.record_outcome(P, M, 0, 401, "unauthorized")
    assert b.status_extras()["dimmed"] == []
    # No state tracked either — resolver skip stays inert when disabled.
    assert b.state == {}


def test_disabled_breaker_still_tracks_selection():
    b = _breaker(enabled=False)
    b.record_selection(P, M, 0)
    used = b.status_extras()["last_used"]
    assert len(used) == 1
    assert used[0]["conn_index"] == 0


# ── 8. status_extras shape ──────────────────────────────────────────────────

def test_status_extras_shape():
    b = _breaker()
    b.record_outcome(P, M, 0, 429, "rate limited")
    b.record_selection(P, M, 0)
    extras = b.status_extras()
    assert set(extras.keys()) == {"dimmed", "last_used"}
    d = extras["dimmed"][0]
    assert set(d.keys()) == {"provider", "conn_index", "since", "age_seconds"}
    u = extras["last_used"][0]
    assert set(u.keys()) == {"provider", "conn_index", "ts", "age_seconds", "model"}


# ── 9. Real-breaker integration: resolve_active_connection + CircuitBreaker ──
# (Hy3 re-audit + GLM cross-check, 2026-08-28: every test above asserts
# breaker internals directly, and the pre-existing resolver tests use STUB
# breakers. These lock the end-to-end chain a real 429 travels:
#   record_outcome → state=OPEN → filter_healthy_connections → resolver skips


def _resolver_cfg():
    """Two-key provider, no models metadata (legacy path: all keys eligible)."""
    return {
        "providers": {
            P: {
                "connections": [
                    {"api_key": "k0"},
                    {"api_key": "k1"},
                ],
            },
        },
    }


def test_resolver_skips_open_key_real_breaker():
    b = _breaker()  # enabled, threshold 3, recovery 30
    b.record_outcome(P, M, 0, 429, "rate limited")  # key 0 → OPEN + dim
    conn, idx = resolve_active_connection(_resolver_cfg(), P, M, breaker=b)
    assert idx == 1  # OPEN key skipped, sibling picked


def test_resolver_top_first_when_breaker_disabled():
    b = _breaker(enabled=False)
    b.record_outcome(P, M, 0, 429, "rate limited")  # no-op when disabled
    conn, idx = resolve_active_connection(_resolver_cfg(), P, M, breaker=b)
    assert idx == 0  # breaker inert → deterministic top-first


def test_resolver_readmits_key_after_recovery_timeout():
    # recovery=0 → open_until == now → expired on the next is_open() check,
    # which transitions OPEN → HALF_OPEN and lets the probe flow.
    b = _breaker(recovery=0)
    b.record_outcome(P, M, 0, 429, "rate limited")
    # Dim display LAGS recovery by design: it only clears on a real success
    # (or admin reset) — the resolver has already readmitted the key.
    assert len(b.status_extras()["dimmed"]) == 1
    conn, idx = resolve_active_connection(_resolver_cfg(), P, M, breaker=b)
    assert idx == 0  # HALF_OPEN probe allowed, top-first wins
