"""Continuous fallback: chain-expansion + wall-clock stop for all-429 chains.

Regression target: a combo whose every entry answers 429
("rate_limited_by_admin") used to terminate after ONE pass with
"All N combo chain entries exhausted", because all 24 fallback sites in
main.py gate recursion on `_next_idx < len(active_chain)`.
"""
import time

import pytest

from app.routing.combo_resolver import (
    CHAIN_MAX_ATTEMPTS,
    CHAIN_MAX_PASSES,
    CHAIN_WALL_BUDGET,
    advance_combo_retry,
    count_eligible_keys,
    expand_chain_for_retries,
)


def _cfg(keys_by_provider, indexes_by_model=None):
    providers = {}
    for prov, n in keys_by_provider.items():
        providers[prov] = {
            "connections": [{"api_key": f"k{i}", "enabled": True} for i in range(n)],
            "models": [{"id": f"{prov}-model"}],
        }
    if indexes_by_model:
        for prov, idxs in indexes_by_model.items():
            providers[prov]["models"][0]["connection_indexes"] = idxs
    return {"providers": providers}


# -- count_eligible_keys ------------------------------------------------------

def test_count_keys_plain():
    assert count_eligible_keys(_cfg({"p": 3}), "p", "p-model") == 3


def test_count_keys_respects_connection_indexes():
    cfg = _cfg({"p": 5}, indexes_by_model={"p": [1, 3]})
    assert count_eligible_keys(cfg, "p", "p-model") == 2


def test_count_keys_ignores_disabled():
    cfg = _cfg({"p": 3})
    cfg["providers"]["p"]["connections"][1]["enabled"] = False
    assert count_eligible_keys(cfg, "p", "p-model") == 2


def test_count_keys_unknown_provider_is_zero():
    assert count_eligible_keys(_cfg({"p": 1}), "nope", "x") == 0


# -- expand_chain_for_retries -------------------------------------------------

def test_multi_entry_chain_gets_second_pass():
    chain = [("a", "p", None), ("b", "p", None)]
    out, passes = expand_chain_for_retries(chain, _cfg({"p": 1}), min_passes=2)
    assert passes == 2
    assert len(out) == 4
    assert out[0] == out[2] and out[1] == out[3]


def test_single_model_combo_still_retries():
    """Reported case B: a combo containing ONE model must not hard-stop.

    Qwen3.8-Max in the live config is a 1-entry, 1-key combo. Sizing passes on
    chain length gave it exactly one attempt.
    """
    chain = [("m", "p", None)]
    out, passes = expand_chain_for_retries(chain, _cfg({"p": 1}), min_passes=2)
    assert passes == 2
    assert out == [("m", "p", None), ("m", "p", None)]


def test_last_entry_failure_wraps_to_top():
    """Reported case A: failing the BOTTOM entry wraps to the TOP entry."""
    chain = [("top", "p", None), ("mid", "p", None), ("bottom", "p", None)]
    expanded, _ = expand_chain_for_retries(chain, _cfg({"p": 1}), min_passes=2)

    # index 2 is the bottom entry of pass 1; the next index must be the TOP again
    assert expanded[2][0] == "bottom"
    assert expanded[3][0] == "top"

    state = {"chain": expanded, "idx": 3}
    adv = advance_combo_retry(state, _cfg({"p": 1}))
    assert not adv.exhausted
    assert adv.target_model == "top"


def test_single_key_direct_model_unchanged():
    """min_passes=1 + 1 key must reproduce the exact old behaviour."""
    chain = [("m", "p", None)]
    out, passes = expand_chain_for_retries(chain, _cfg({"p": 1}), min_passes=1)
    assert passes == 1
    assert out == chain


def test_passes_sized_by_widest_key_pool():
    chain = [("p1-model", "p1", None), ("p2-model", "p2", None)]
    out, passes = expand_chain_for_retries(chain, _cfg({"p1": 1, "p2": 4}), min_passes=1)
    assert passes == 4
    assert len(out) == 8


def test_passes_clamped_by_max_passes():
    chain = [("m", "p", None)]
    out, passes = expand_chain_for_retries(chain, _cfg({"p": 50}), min_passes=1)
    assert passes == CHAIN_MAX_PASSES


def test_long_chain_still_gets_guaranteed_pass():
    """A chain longer than CHAIN_MAX_ATTEMPTS must NOT lose its retry pass.

    Regression: clamping by `max_attempts // base_len` made 24 // 37 == 0, which
    silently disabled continuous fallback for coder-1/coder-2/coder-3 (the very
    combos the user reported). The wall clock bounds these instead.
    """
    chain = [("m", "p", None)] * 37
    out, passes = expand_chain_for_retries(chain, _cfg({"p": 1}), min_passes=2)
    assert passes == 2
    assert len(out) == 74


def test_ceiling_still_limits_mid_length_chains():
    """Above the guaranteed minimum, the attempt ceiling still applies."""
    chain = [("m", "p", None)] * 6
    out, passes = expand_chain_for_retries(chain, _cfg({"p": 8}), min_passes=2)
    assert passes == CHAIN_MAX_ATTEMPTS // 6  # 4, not the 6 keys would suggest
    assert len(out) <= CHAIN_MAX_ATTEMPTS


def test_empty_chain_is_safe():
    out, passes = expand_chain_for_retries([], _cfg({"p": 3}))
    assert out == [] and passes == 1


def test_expansion_preserves_entry_tuples():
    chain = [("m", "p", "high")]
    out, _ = expand_chain_for_retries(chain, _cfg({"p": 2}), min_passes=2)
    assert all(e == ("m", "p", "high") for e in out)


# -- cycling behaviour through advance_combo_retry ----------------------------

def test_expanded_chain_revisits_same_leaf_on_second_pass():
    """The core fix: idx L..2L-1 re-dials the same leaves instead of stopping."""
    chain = [("a", "p", None), ("b", "p", None)]
    expanded, _ = expand_chain_for_retries(chain, _cfg({"p": 2}), min_passes=2)
    cfg = _cfg({"p": 2})

    seen = []
    for idx in range(len(expanded)):
        state = {"chain": expanded, "idx": idx}
        adv = advance_combo_retry(state, cfg)
        assert not adv.exhausted
        seen.append((adv.provider_name, adv.target_model))

    assert seen == [("p", "a"), ("p", "b"), ("p", "a"), ("p", "b")]


def test_exhaustion_still_reported_at_true_end():
    chain = [("a", "p", None)]
    state = {"chain": chain, "idx": 1}
    assert advance_combo_retry(state, _cfg({"p": 1})).exhausted


# -- wall-clock stop (C7) -----------------------------------------------------

# -- chain-sized wall budget (2026-08-24: force-stop-instead-of-retry report) --

def test_wall_budget_scales_with_chain():
    """Flat 240s stranded entries 3+4 of a 4-entry chain whose leaves each
    burned ~125s on Cloudflare 524s. Every entry must get one full burn."""
    from app.routing.combo_resolver import wall_budget_for_chain
    assert wall_budget_for_chain(0) == CHAIN_WALL_BUDGET       # floor
    assert wall_budget_for_chain(1) == CHAIN_WALL_BUDGET       # floor still (130 < 240)
    assert wall_budget_for_chain(2) == 2 * 130.0               # scaling wins at 2 entries (260 > 240)
    assert wall_budget_for_chain(4) == 4 * 130.0               # Opus-Tabitoken
    assert wall_budget_for_chain(24) == 24 * 130.0             # attempt ceiling


def test_slow_chain_not_stranded_by_wall():
    """Opus-Tabitoken shape: 2 leaves x 2 passes, each ~125s. After entry 2
    (elapsed ~250s), the advance must STILL allow entry 3 — the flat 240s
    budget force-stopped here, reporting 'All 4 exhausted' with 2 untried."""
    from app.routing.combo_resolver import wall_budget_for_chain
    chain = [("claude-opus-5-thinking", "tabitoken", None),
             ("claude-opus-4-8-thinking", "tabitoken", None)] * 2
    state = {"chain": chain, "idx": 2}
    wall_start = time.monotonic() - 250.0  # after two ~125s leaf burns
    adv = advance_combo_retry(state, _cfg({"tabitoken": 1}), wall_start=wall_start)
    assert not adv.exhausted
    assert adv.target_model == "claude-opus-5-thinking"  # entry 3 = pass 2 of leaf 1


def test_wall_still_stops_pathological_loops():
    """Even chain-sized, the wall must eventually stop an endless loop of
    slow failures — e.g. 4 entries x 130s budget after 600s elapsed."""
    chain = [("m1", "p", None), ("m2", "p", None)] * 2
    state = {"chain": chain, "idx": 2}
    wall_start = time.monotonic() - 600.0  # far past 4 x 130 = 520
    adv = advance_combo_retry(state, _cfg({"p": 1}), wall_start=wall_start)
    assert adv.exhausted


def test_wall_uses_fallback_budget_without_chain():
    """wall_start set but _retry_state has no chain (defensive) -> floor budget."""
    chain = [("m", "p", None)]
    state = {"chain": chain, "idx": 0}
    wall_start = time.monotonic() - 300.0  # past floor 240, under 130 * 1... floor wins
    adv = advance_combo_retry(state, _cfg({"p": 1}), wall_start=wall_start)
    assert adv.exhausted  # 300 > max(240, 130) -> wall fires


def test_wall_uses_fallback_budget_without_chain_lower():
    """1-entry chain below floor: 300s elapsed still trips (240 floor)."""
    chain = [("m", "p", None)] * 1
    state = {"chain": chain, "idx": 0}
    wall_start = time.monotonic() - 300.0
    adv = advance_combo_retry(state, _cfg({"p": 1}), wall_start=wall_start)
    assert adv.exhausted


def test_wall_no_chain_sizes_from_snapshot():
    """Exhausted-check uses the SNAPSHOT chain in _retry_state (C2), and the
    budget derives from that same snapshot — proven by exhausting past it."""
    chain = [("m1", "p", None), ("m2", "p", None)] * 2
    state = {"chain": chain, "idx": 3}  # last entry
    wall_start = time.monotonic() - (4 * 130.0 + 10)  # past chain-sized budget
    adv = advance_combo_retry(state, _cfg({"p": 1}), wall_start=wall_start)
    assert adv.exhausted


def test_wall_clock_allows_retries_inside_budget():
    chain = [("a", "p", None), ("b", "p", None)]
    state = {"chain": chain, "idx": 0}
    adv = advance_combo_retry(state, _cfg({"p": 1}), wall_start=time.monotonic())
    assert not adv.exhausted
    assert adv.target_model == "a"


def test_wall_clock_omitted_is_backward_compatible():
    chain = [("a", "p", None)]
    state = {"chain": chain, "idx": 0}
    adv = advance_combo_retry(state, _cfg({"p": 1}))
    assert not adv.exhausted


def test_wall_clock_custom_budget():
    chain = [("a", "p", None)]
    state = {"chain": chain, "idx": 0}
    adv = advance_combo_retry(
        state, _cfg({"p": 1}), wall_start=time.monotonic() - 5.0, wall_budget=2.0
    )
    assert adv.exhausted
