"""Regression tests for multi-key failover (KEY FAILOVER FIX 2026-08-27).

Proves that a failed first key advances to sibling keys (same leaf) and then
combo chain entries, instead of dead-ending the request.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.utils.model_resolver import untried_connection_count  # noqa: E402


def _cfg(n_keys=3, indexes=None):
    conns = [{"api_key": f"k{i}", "enabled": True} for i in range(n_keys)]
    model = {"id": "m1", "connection_indexes": indexes if indexes is not None else list(range(n_keys))}
    return {
        "error_prevention": {"enabled": True},
        "providers": {"p1": {"connections": conns, "models": [model]}},
    }


def test_untried_counts_all_keys_initially():
    cfg = _cfg(3)
    assert untried_connection_count(cfg, "p1", "m1") == 3


def test_untried_drops_tried_keys():
    cfg = _cfg(3)
    assert untried_connection_count(cfg, "p1", "m1", {0}) == 2
    assert untried_connection_count(cfg, "p1", "m1", {0, 1}) == 1
    assert untried_connection_count(cfg, "p1", "m1", {0, 1, 2}) == 0


def test_untried_respects_connection_indexes():
    # model pinned to keys [0,2]; key 0 tried -> 1 remains
    cfg = _cfg(3, indexes=[0, 2])
    assert untried_connection_count(cfg, "p1", "m1", {0}) == 1
    assert untried_connection_count(cfg, "p1", "m1", {0, 2}) == 0


def test_untried_ignores_disabled_keys():
    cfg = _cfg(3)
    cfg["providers"]["p1"]["connections"][1]["enabled"] = False
    assert untried_connection_count(cfg, "p1", "m1", {0}) == 1  # only key 2 left


def test_untried_unknown_provider_or_model():
    cfg = _cfg(2)
    assert untried_connection_count(cfg, "nope", "m1") == 0
    # no matching model -> legacy path (all enabled keys eligible)
    assert untried_connection_count(cfg, "p1", "other") == 2


def test_advance_retry_keeps_banned_leaf_with_untried_keys():
    """RC5 exemption: banned leaf with tried keys + sibling keys -> NOT skipped."""
    import app.error_prevention as ep
    from app.routing.combo_resolver import advance_combo_retry

    cfg = _cfg(3)
    # Ban p1/m1 (simulating the zero-strike softban from a failed key 0).
    ep.record_outcome(cfg, "p1", "m1", 429, "rate_limited")
    banned, _, _ = ep.check_ban(cfg, "p1", "m1")
    assert banned, "precondition: leaf must be banned"

    state = {
        "chain": [("m1", "p1", None), ("m2", "p2", None)],
        "idx": 0,
        "tried_conns": {"p1": [0]},
    }
    adv = advance_combo_retry(state, cfg)
    assert not adv.exhausted
    assert adv.target_model == "m1" and adv.provider_name == "p1", (
        "banned leaf with untried sibling keys must be re-selected for key rotation"
    )


def test_advance_retry_skips_banned_leaf_without_tried_keys():
    """Banned leaf that this request never dialed stays skipped (historical RC5)."""
    import app.error_prevention as ep
    from app.routing.combo_resolver import advance_combo_retry

    cfg = {"error_prevention": {"enabled": True}, "providers": {"p1": _cfg(3)["providers"]["p1"], "p2": _cfg(1)["providers"]["p1"]}}
    ep.record_outcome(cfg, "p1", "m1", 429, "rate_limited")

    state = {"chain": [("m1", "p1", None), ("m2", "p2", None)], "idx": 0}
    adv = advance_combo_retry(state, cfg)
    assert not adv.exhausted
    assert adv.provider_name == "p2", "fresh-request banned leaf must stay skipped"


def test_advance_retry_skips_when_all_keys_tried():
    """Banned leaf with ALL keys tried -> skip (rotation room exhausted)."""
    import app.error_prevention as ep
    from app.routing.combo_resolver import advance_combo_retry

    cfg = _cfg(2)
    ep.record_outcome(cfg, "p1", "m1", 429, "rate_limited")

    state = {
        "chain": [("m1", "p1", None), ("m2", "p2", None)],
        "idx": 0,
        "tried_conns": {"p1": [0, 1]},
    }
    adv = advance_combo_retry(state, cfg)
    assert not adv.exhausted
    assert adv.provider_name == "p2"
