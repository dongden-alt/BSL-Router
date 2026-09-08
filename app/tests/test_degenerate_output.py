"""Lane 1: Degenerate-output guard + effort-cap (gpt-family / vsllm-r).

Evidence (2026-09-06, production): vsllm-r gpt upstream returns HTTP 200
with 9-112 tokens of reasoning rubble at effort:xhigh when input is
42k-63k tokens. Small test payloads do NOT reproduce it. The guard:

  1. PREVENTION — cap xhigh/max -> high pre-egress when input >= floor
     (scoped to vsllm-r + openai-responses wire + a per-model set).
  2. DETECTION — flag finished 200 responses matching the rubble
     signature via obs.note_degenerate_output + a `degenerate` flag on
     the END console row (telemetry only; no retry after emission).

Per-model scope (2026-09-07 probe, 12 uncapped max calls): sol PASS 4/4
at every size, astra EMPTY 4/4 on an upstream 524/502/429 storm an effort
cap cannot fix, terra RUBBLE 4/4 — so the cap defaults to
tools.degenerate_output_guard.models == ("gpt-5.6-terra",) only, with
per-model overrides flipping membership + tuning and a Providers-UI
"Cap 40K→high" chip deriving its state from the same config.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.observability as obs
from app.compat.reasoning_policy import (
    DEGENERATE_EFFORT_CAP,
    DEGENERATE_GUARD_MODELS_DEFAULT,
    DEGENERATE_INPUT_TOKEN_FLOOR,
    DEGENERATE_OUTPUT_TOKEN_CEILING,
    EFFORT_LADDER_DEFAULT,
    canonical_model_id,
    cap_effort_for_input_size,
    degenerate_cap_chip_state,
    degenerate_guard_config,
    is_degenerate_output,
)


# ─────────────────────────────────────────────────────────────────────
# Pure predicates: cap_effort_for_input_size
# ─────────────────────────────────────────────────────────────────────


def test_cap_fires_at_floor_xhigh():
    effort, capped = cap_effort_for_input_size("xhigh", 42000)
    assert capped is True
    assert effort == "high"


def test_cap_fires_at_floor_max():
    effort, capped = cap_effort_for_input_size("max", 63000)
    assert capped is True
    assert effort == "high"


def test_no_cap_below_floor():
    effort, capped = cap_effort_for_input_size("xhigh", 12000)
    assert capped is False
    assert effort == "xhigh"


def test_no_cap_for_high_effort():
    # "high" has not been observed to degenerate — pass through untouched.
    effort, capped = cap_effort_for_input_size("high", 50000)
    assert capped is False
    assert effort == "high"


def test_no_cap_for_medium_low():
    for e in ("medium", "low"):
        effort, capped = cap_effort_for_input_size(e, 63000)
        assert capped is False
        assert effort == e


def test_no_cap_for_off_values():
    for e in ("auto", "none", "off", "", None):
        effort, capped = cap_effort_for_input_size(e, 63000)
        assert capped is False
        assert effort in ("auto", "none", "off", "")


def test_cap_case_insensitive_and_strips():
    effort, capped = cap_effort_for_input_size("  XHIGH  ", 50000)
    assert capped is True
    assert effort == DEGENERATE_EFFORT_CAP == "high"


def test_cap_garbage_input_tokens_is_noop():
    for bad in (None, "abc", [], {}):
        effort, capped = cap_effort_for_input_size("xhigh", bad)
        assert capped is False
        assert effort == "xhigh"


# ─────────────────────────────────────────────────────────────────────
# Graduated effort ladder: cap_effort_for_input_size(ladder=...)
# ─────────────────────────────────────────────────────────────────────


def test_ladder_two_tier_boundaries():
    # {40000: high, 100000: medium}: exact floor is INclusive on each tier,
    # the token below falls to the next lower tier (or passes through).
    ladder = ((40000, "high"), (100000, "medium"))
    for n_in, want_cap, want_capped in (
        (39999, None, False),   # below every tier -> passthrough
        (40000, "high", True),  # hits the lower tier exactly
        (99999, "high", True),  # still the lower tier (DESC walk)
        (100000, "medium", True),  # hits the higher tier exactly
    ):
        effort, capped = cap_effort_for_input_size("xhigh", n_in, ladder=ladder)
        assert capped is want_capped
        if want_capped:
            assert effort == want_cap
        else:
            assert effort == "xhigh"


def test_ladder_unsorted_config_sorted_desc_internally():
    # Config order must not matter: tiers are walked by floor DESC, so the
    # highest matching tier wins regardless of declaration order.
    ladder = ((100000, "medium"), (40000, "high"))
    effort, capped = cap_effort_for_input_size("max", 100000, ladder=ladder)
    assert capped is True
    assert effort == "medium"  # 100k tier beats the 40k tier
    effort, capped = cap_effort_for_input_size("max", 50000, ladder=ladder)
    assert capped is True
    assert effort == "high"


def test_ladder_empty_is_explicit_opt_out():
    # [] = never caps, even at degenerate-scale input.
    for e in ("xhigh", "max"):
        effort, capped = cap_effort_for_input_size(e, 500000, ladder=())
        assert capped is False
        assert effort == e


def test_ladder_invalid_entries_dropped_valid_kept():
    # Malformed tiers (wrong shape, negative floor, empty cap) are skipped
    # defensively; the one valid (40000, "high") tier survives and caps.
    # Dict-shaped entries are the config parser's format — the pure function
    # takes (floor, cap) sequences, so a dict here is just malformed shape.
    ladder = (
        "nope",            # not a 2-sequence
        (-5, "high"),      # negative floor
        (40000, ""),       # empty cap
        (40000, "high"),   # valid
    )
    effort, capped = cap_effort_for_input_size("xhigh", 42000, ladder=ladder)
    assert capped is True
    assert effort == "high"


def test_ladder_garbage_ladder_type_falls_back_to_default(capsys):
    # A non-list/non-tuple ladder falls back to EFFORT_LADDER_DEFAULT with
    # one console warn — never a crash, never silent capping with junk.
    effort, capped = cap_effort_for_input_size("xhigh", 50000, ladder="big")
    assert capped is True
    assert effort == "high"
    out = capsys.readouterr().out
    assert out.count("effort_ladder must be a list/tuple") == 1


def test_ladder_none_uses_default_flat_behavior():
    # ladder=None (the default) must reproduce today's flat behavior exactly.
    assert EFFORT_LADDER_DEFAULT == ((DEGENERATE_INPUT_TOKEN_FLOOR, DEGENERATE_EFFORT_CAP),)
    assert cap_effort_for_input_size("xhigh", 42000, ladder=None) == ("high", True)
    assert cap_effort_for_input_size("xhigh", 39999, ladder=None) == ("xhigh", False)


# ─────────────────────────────────────────────────────────────────────
# Pure predicates: is_degenerate_output
# ─────────────────────────────────────────────────────────────────────


def test_degenerate_observed_band():
    # The production signature: 42k-63k in, 9-112 out.
    assert is_degenerate_output(42000, 9) is True
    assert is_degenerate_output(63000, 112) is True
    assert is_degenerate_output(50000, 50) is True


def test_degenerate_zero_output_counts():
    assert is_degenerate_output(50000, 0) is True


def test_healthy_output_not_degenerate():
    assert is_degenerate_output(50000, 2000) is False
    assert is_degenerate_output(50000, DEGENERATE_OUTPUT_TOKEN_CEILING + 1) is False


def test_small_input_not_degenerate_even_with_rubble_out():
    assert is_degenerate_output(12000, 10) is False


def test_degenerate_ceiling_boundary():
    assert is_degenerate_output(DEGENERATE_INPUT_TOKEN_FLOOR, DEGENERATE_OUTPUT_TOKEN_CEILING) is True
    assert is_degenerate_output(DEGENERATE_INPUT_TOKEN_FLOOR - 1, DEGENERATE_OUTPUT_TOKEN_CEILING) is False


def test_degenerate_garbage_tokens_false():
    for bad_in, bad_out in ((None, 0), ("x", "y"), (50000, "many"), ([], {})):
        assert is_degenerate_output(bad_in, bad_out) is False


# ─────────────────────────────────────────────────────────────────────
# Variant-ID canonicalization: canonical_model_id
# ─────────────────────────────────────────────────────────────────────


def test_canonical_model_id_dash_folds_to_dot():
    # Reuses the families/_base.py ThinkingContext choke point (match-only).
    assert canonical_model_id("gpt-5-6-terra") == "gpt-5.6-terra"
    assert canonical_model_id("gpt-5.6-terra") == "gpt-5.6-terra"
    assert canonical_model_id("glm-5-3") == "glm-5.3"


def test_canonical_model_id_word_dashes_preserved():
    # Word dashes are unaffected: the pattern requires digits on both sides.
    assert canonical_model_id("gpt-6-astra") == "gpt-6-astra"
    assert canonical_model_id("kimi-k3") == "kimi-k3"


def test_canonical_model_id_normalizes_case_and_space():
    assert canonical_model_id("  GPT-5-6-Terra  ") == "gpt-5.6-terra"
    assert canonical_model_id(None) == ""
    assert canonical_model_id(123) == "123"


# ─────────────────────────────────────────────────────────────────────
# Config normalization: degenerate_guard_config
# ─────────────────────────────────────────────────────────────────────


def test_guard_config_defaults_enabled():
    cfg = degenerate_guard_config({})
    assert cfg["enabled"] is True
    assert cfg["effort_cap"] == "high"
    assert cfg["input_token_floor"] == DEGENERATE_INPUT_TOKEN_FLOOR
    assert cfg["output_token_ceiling"] == DEGENERATE_OUTPUT_TOKEN_CEILING
    # 2026-09-07 per-model scope defaults: terra only, no overrides.
    assert cfg["models"] == DEGENERATE_GUARD_MODELS_DEFAULT == ("gpt-5.6-terra",)
    assert cfg["model_overrides"] == {}


def test_guard_config_bool_switch():
    assert degenerate_guard_config({"tools": {"degenerate_output_guard": False}})["enabled"] is False
    assert degenerate_guard_config({"tools": {"degenerate_output_guard": True}})["enabled"] is True
    # Bool switch keeps the per-model scope defaults.
    assert degenerate_guard_config({"tools": {"degenerate_output_guard": False}})["models"] == DEGENERATE_GUARD_MODELS_DEFAULT


def test_guard_config_dict_overrides():
    cfg = degenerate_guard_config(
        {
            "tools": {
                "degenerate_output_guard": {
                    "enabled": True,
                    "effort_cap": "MEDIUM",
                    "input_token_floor": 30000,
                    "output_token_ceiling": 150,
                }
            }
        }
    )
    assert cfg == {
        "enabled": True,
        "effort_cap": "medium",
        "input_token_floor": 30000,
        "output_token_ceiling": 150,
        "models": DEGENERATE_GUARD_MODELS_DEFAULT,
        "model_overrides": {},
    }


def test_guard_config_bad_values_fall_back():
    cfg = degenerate_guard_config(
        {"tools": {"degenerate_output_guard": {"input_token_floor": "big", "output_token_ceiling": None}}}
    )
    assert cfg["input_token_floor"] == DEGENERATE_INPUT_TOKEN_FLOOR
    assert cfg["output_token_ceiling"] == DEGENERATE_OUTPUT_TOKEN_CEILING


# ─────────────────────────────────────────────────────────────────────
# Config normalization: per-model scope keys (models / model_overrides)
# ─────────────────────────────────────────────────────────────────────


def _gcfg(**kw):
    """Raw config value -> normalized guard cfg (the path main.py uses)."""
    return degenerate_guard_config({"tools": {"degenerate_output_guard": kw}})


def test_guard_config_models_absent_keeps_default():
    assert _gcfg()["models"] == DEGENERATE_GUARD_MODELS_DEFAULT


def test_guard_config_models_explicit_canonicalized():
    cfg = _gcfg(models=["gpt-5-6-sol", "gpt-5.6-terra", "GPT-6-Astra"])
    assert cfg["models"] == ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra")


def test_guard_config_models_empty_is_opt_out():
    # [] = explicit opt-out: the cap never applies (kept as (), NOT default).
    assert _gcfg(models=[])["models"] == ()


def test_guard_config_models_invalid_entries_dropped_one_warn(capsys):
    cfg = _gcfg(models=["gpt-5.6-sol", 7, ""])
    # Valid entries survive; non-str / empty entries are dropped.
    assert cfg["models"] == ("gpt-5.6-sol",)
    out = capsys.readouterr().out
    assert out.count("[DegenerateGuard] model scope config dropped") == 1
    assert "2 invalid models entries" in out


def test_guard_config_models_non_list_warns_keeps_default(capsys):
    cfg = _gcfg(models="terra")
    assert cfg["models"] == DEGENERATE_GUARD_MODELS_DEFAULT
    out = capsys.readouterr().out
    assert out.count("[DegenerateGuard] model scope config dropped") == 1
    assert "models must be a list" in out


def test_guard_config_model_overrides_parsed_and_canonicalized():
    cfg = _gcfg(
        model_overrides={
            "gpt-5-6-terra": {"enabled": False, "effort_cap": "MEDIUM", "input_token_floor": 25000},
            "gpt-6-astra": {"enabled": True, "effort_ladder": [{"floor": 50000, "cap": "High"}]},
            "sol-no-flags": {"effort_cap": "low"},  # no enabled key -> kept, inert membership
        }
    )
    assert set(cfg["model_overrides"]) == {"gpt-5.6-terra", "gpt-6-astra", "sol-no-flags"}
    assert cfg["model_overrides"]["gpt-5.6-terra"] == {
        "enabled": False, "effort_cap": "medium", "input_token_floor": 25000,
    }
    assert cfg["model_overrides"]["gpt-6-astra"] == {
        "enabled": True, "effort_ladder": ((50000, "high"),),
    }
    assert cfg["model_overrides"]["sol-no-flags"] == {"effort_cap": "low"}


def test_guard_config_model_overrides_invalid_shapes_one_warn(capsys):
    cfg = _gcfg(
        model_overrides={
            "gpt-5.6-terra": "junk",       # non-dict sub-value -> dropped
            "": {"enabled": True},          # empty model id -> dropped
            9: {"enabled": True},           # non-str key -> dropped
            "gpt-5.6-sol": {"enabled": True, "input_token_floor": "big"},  # bad floor key
        }
    )
    # Entry-level invalidity (terra, empty, int key) drops the whole entry.
    # The sol entry survives with its valid `enabled` key; only the invalid
    # input_token_floor key is dropped from it (that key-drop counts too).
    # FOUR shapes dropped total; the sol override itself is kept.
    assert cfg["model_overrides"] == {"gpt-5.6-sol": {"enabled": True}}
    out = capsys.readouterr().out
    # ONE warn line total, regardless of how many shapes were dropped.
    assert out.count("[DegenerateGuard] model scope config dropped") == 1
    assert "4 invalid model_overrides shapes" in out


def test_guard_config_model_overrides_bad_ladder_counts_into_warn(capsys):
    cfg = _gcfg(
        model_overrides={
            "gpt-5.6-terra": {
                "effort_ladder": [
                    {"floor": -1, "cap": "high"},          # invalid tier
                    {"floor": 30000, "cap": "medium"},     # valid tier
                ]
            }
        }
    )
    assert cfg["model_overrides"]["gpt-5.6-terra"]["effort_ladder"] == ((30000, "medium"),)
    out = capsys.readouterr().out
    assert out.count("[DegenerateGuard] model scope config dropped") == 1


def test_guard_config_model_overrides_non_dict_warns(capsys):
    cfg = _gcfg(model_overrides=["gpt-5.6-terra"])
    assert cfg["model_overrides"] == {}
    out = capsys.readouterr().out
    assert out.count("[DegenerateGuard] model scope config dropped") == 1
    assert "model_overrides must be a dict" in out


def test_guard_config_model_overrides_absent_no_key_content():
    # Absent overrides stay an EMPTY dict (never None) so decision-side
    # .get() chains are safe.
    assert _gcfg()["model_overrides"] == {}


# ─────────────────────────────────────────────────────────────────────
# Lane gating: _degenerate_effort_cap_decision (imports app.main)
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def decision():
    import app.main as main  # noqa: F401 — heavy import; conftest covers config

    return main._degenerate_effort_cap_decision


def _cfg(**over):
    base = degenerate_guard_config({})
    base.update(over)
    return base


def test_cap_decision_fires_on_terra_default(decision):
    # 2026-09-07 default scope: gpt-5.6-terra only (probe: terra RUBBLE 4/4).
    d = decision(
        "gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, _cfg()
    )
    assert d is not None
    assert d["from"] == "xhigh"
    assert d["to"] == "high"
    assert d["input_tokens"] == 50000
    assert d["model"] == "gpt-5.6-terra"


def test_cap_decision_default_scope_terra_only(decision):
    # The rest of the vsllm-r gpt family is NOT capped by default anymore:
    # sol PASSed 4/4 uncapped and astra failed on an upstream storm, so an
    # effort cap is pointless there (replaces the old gpt-5 family gate).
    for model in ("gpt-5.6-sol", "gpt-6-astra", "gpt-5.6-luna"):
        assert (
            decision(
                model, "vsllm-r", "openai-responses", "xhigh", 50000, _cfg()
            )
            is None
        )
    # ...while the scoped model still caps.
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-r", "openai-responses", "max", 63000, _cfg()
        )
        is not None
    )


def test_cap_decision_fires_on_dash_variant_model_id(decision):
    # Config variant ids use dashes (gpt-5-6-terra); canonical_model_id folds
    # them onto the dotted canonical the models set stores.
    assert (
        decision(
            "gpt-5-6-terra", "vsllm-r", "openai-responses", "max", 63000, _cfg()
        )
        is not None
    )


def test_cap_decision_other_provider_untouched(decision):
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-a", "openai-responses", "xhigh", 50000, _cfg()
        )
        is None
    )


def test_cap_decision_other_wire_untouched(decision):
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-r", "openai", "xhigh", 50000, _cfg()
        )
        is None
    )


def test_cap_decision_small_input_untouched(decision):
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 12000, _cfg()
        )
        is None
    )


def test_cap_decision_high_effort_untouched(decision):
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-r", "openai-responses", "high", 50000, _cfg()
        )
        is None
    )


def test_cap_decision_auto_untouched(decision):
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-r", "openai-responses", "auto", 50000, _cfg()
        )
        is None
    )


def test_cap_decision_kill_switch(decision):
    assert (
        decision(
            "gpt-5.6-terra",
            "vsllm-r",
            "openai-responses",
            "xhigh",
            50000,
            _cfg(enabled=False),
        )
        is None
    )


def test_cap_decision_custom_floor_and_cap(decision):
    d = decision(
        "gpt-5.6-terra",
        "vsllm-r",
        "openai-responses",
        "max",
        35000,
        _cfg(input_token_floor=30000, effort_cap="medium"),
    )
    assert d is not None
    assert d["to"] == "medium"
    assert d["floor"] == 30000
    # Legacy flat keys = single back-compat tier: the tier IS the flat floor.
    assert d["tier_floor"] == 30000


# ─────────────────────────────────────────────────────────────────────
# Per-model scope: explicit models set + overrides (2026-09-07)
# ─────────────────────────────────────────────────────────────────────


def test_cap_decision_explicit_models_sol_not_terra(decision):
    cfg = _gcfg(models=["gpt-5.6-sol"])
    assert (
        decision("gpt-5.6-sol", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is not None
    )
    # Terra dropped off the list -> untouched even at degenerate-scale input.
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is None
    )


def test_cap_decision_models_empty_opt_out(decision):
    # models: [] = explicit opt-out — even 500k input passes xhigh through.
    cfg = _gcfg(models=[])
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 500000, cfg)
        is None
    )


def test_cap_decision_models_invalid_falls_back_to_default(decision, capsys):
    # A non-list models value is dropped fail-open -> default (terra only).
    cfg = _gcfg(models="terra")
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is not None
    )
    assert (
        decision("gpt-5.6-sol", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is None
    )
    assert capsys.readouterr().out.count("[DegenerateGuard] model scope config dropped") == 1


def test_cap_decision_dash_variant_models_list(decision):
    # Dash ids in the models list canonicalize, so dotted + dashed requests
    # both match the same entry.
    cfg = _gcfg(models=["gpt-5-6-terra"])
    assert cfg["models"] == ("gpt-5.6-terra",)
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is not None
    )
    assert (
        decision("gpt-5-6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is not None
    )


def test_cap_decision_override_enabled_false_disables_terra(decision):
    # Terra is in the global set; an enabled=false override removes it.
    cfg = _cfg(model_overrides={"gpt-5.6-terra": {"enabled": False}})
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is None
    )


def test_cap_decision_override_enabled_true_enables_astra(decision):
    # Astra is absent from the global set; an enabled=true override adds it.
    cfg = _cfg(model_overrides={"gpt-6-astra": {"enabled": True}})
    d = decision("gpt-6-astra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
    assert d is not None
    assert d["to"] == "high"
    assert d["model"] == "gpt-6-astra"
    # Sol still out of scope — the override only speaks for its own model.
    assert (
        decision("gpt-5.6-sol", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is None
    )


def test_cap_decision_noncanonical_override_key_canonicalized(decision):
    # Override keys canonicalize, so a dash-spelled key controls the dotted
    # canonical model (UI chips write canonical ids; hand-edits may not).
    cfg = _gcfg(model_overrides={"gpt-5-6-terra": {"enabled": False}})
    assert cfg["model_overrides"] == {"gpt-5.6-terra": {"enabled": False}}
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is None
    )


def test_cap_decision_override_without_enabled_is_membership_inert(decision):
    # An override with tuning keys but NO enabled key never changes
    # membership — terra stays capped via the global set, and astra (with
    # tuning only) stays uncapped.
    cfg = _cfg(model_overrides={"gpt-6-astra": {"effort_cap": "low"}})
    assert (
        decision("gpt-6-astra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
        is None
    )
    d = decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
    assert d is not None
    assert d["to"] == "high"


def test_cap_decision_override_ladder_beats_global(decision):
    # Global ladder would NOT cap at 31k (first tier floor 40000); the
    # per-model override ladder (floor 30000) wins and caps to medium.
    cfg = _cfg(
        effort_ladder=((40000, "high"), (100000, "medium")),
        model_overrides={"gpt-5.6-terra": {"effort_ladder": ((30000, "medium"),)}},
    )
    d = decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 31000, cfg)
    assert d is not None
    assert d["to"] == "medium"
    assert d["tier_floor"] == 30000
    # No override floor: the flat floor field stays the global one.
    assert d["floor"] == DEGENERATE_INPUT_TOKEN_FLOOR


def test_cap_decision_override_floor_respected(decision):
    cfg = _cfg(model_overrides={"gpt-5.6-terra": {"input_token_floor": 30000}})
    d = decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 31000, cfg)
    assert d is not None
    assert d["floor"] == 30000
    assert d["tier_floor"] == 30000
    assert d["to"] == "high"  # global cap inherited
    # Below the override floor: untouched (would also be untouched globally,
    # so assert at the band between floors is what proves the override).
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 29000, cfg)
        is None
    )


def test_cap_decision_override_floor_between_bands(decision):
    # 35k sits between the override floor (30000) and the global floor
    # (40000): the override must be the deciding threshold.
    cfg = _cfg(model_overrides={"gpt-5.6-terra": {"input_token_floor": 30000}})
    assert (
        decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 35000, cfg)
        is not None
    )
    assert (
        decision(
            "gpt-5.6-terra",
            "vsllm-r",
            "openai-responses",
            "xhigh",
            35000,
            _cfg(),  # no override -> global floor 40000
        )
        is None
    )


def test_cap_decision_override_flat_cap_single_tier(decision):
    # An override with a flat cap (no ladder) builds a single tier from the
    # global floor + override cap.
    cfg = _cfg(model_overrides={"gpt-5.6-terra": {"effort_cap": "medium"}})
    d = decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
    assert d is not None
    assert d["to"] == "medium"
    assert d["tier_floor"] == DEGENERATE_INPUT_TOKEN_FLOOR


def test_cap_decision_override_no_keys_inherits_global(decision):
    # An override with neither ladder nor flat keys inherits global tuning
    # entirely — here the global effort_cap=medium.
    cfg = _cfg(
        effort_cap="medium",
        model_overrides={"gpt-5.6-terra": {"enabled": True}},
    )
    d = decision("gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 50000, cfg)
    assert d is not None
    assert d["to"] == "medium"
    assert d["tier_floor"] == DEGENERATE_INPUT_TOKEN_FLOOR


# ─────────────────────────────────────────────────────────────────────
# Graduated effort ladder: config parsing + decision wiring + telemetry
# ─────────────────────────────────────────────────────────────────────


def test_guard_config_ladder_parsed_and_normalized():
    cfg = degenerate_guard_config(
        {
            "tools": {
                "degenerate_output_guard": {
                    "effort_ladder": [
                        {"floor": 40000, "cap": "HIGH"},
                        {"floor": 100000, "cap": "Medium"},
                    ]
                }
            }
        }
    )
    assert cfg["effort_ladder"] == ((40000, "high"), (100000, "medium"))
    # Flat keys still normalized alongside for detection-side use.
    assert cfg["effort_cap"] == "high"
    assert cfg["input_token_floor"] == DEGENERATE_INPUT_TOKEN_FLOOR


def test_guard_config_ladder_absent_no_key():
    # Absent effort_ladder must NOT add the key — callers build the legacy
    # single tier from effort_cap/input_token_floor themselves.
    assert "effort_ladder" not in degenerate_guard_config({})
    assert "effort_ladder" not in degenerate_guard_config(
        {"tools": {"degenerate_output_guard": {"effort_cap": "medium"}}}
    )


def test_guard_config_ladder_invalid_entries_dropped_one_warn(capsys):
    cfg = degenerate_guard_config(
        {
            "tools": {
                "degenerate_output_guard": {
                    "effort_ladder": [
                        {"floor": -1, "cap": "high"},   # negative floor
                        {"floor": 40000, "cap": ""},    # empty cap
                        "junk",                          # non-dict
                        {"floor": 100000, "cap": "medium"},  # valid
                    ]
                }
            }
        }
    )
    assert cfg["effort_ladder"] == ((100000, "medium"),)
    # At most ONE console warn line total, regardless of drop count.
    out = capsys.readouterr().out
    assert out.count("[DegenerateGuard] effort_ladder: dropped") == 1


def test_guard_config_ladder_all_invalid_yields_empty_tuple():
    # All entries invalid -> empty tuple == explicit opt-out (never caps),
    # NOT a fall-back to the flat keys (explicit config beats defaults).
    cfg = degenerate_guard_config(
        {
            "tools": {
                "degenerate_output_guard": {
                    "effort_ladder": [{"floor": -1, "cap": "high"}]
                }
            }
        }
    )
    assert cfg["effort_ladder"] == ()


def test_cap_decision_uses_ladder_tiers(decision):
    cfg = _cfg(
        effort_ladder=((40000, "high"), (100000, "medium")),
    )
    # 100k input walks DESC to the medium tier, not the flat default cap.
    d = decision(
        "gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 100000, cfg
    )
    assert d is not None
    assert d["to"] == "medium"
    assert d["tier_floor"] == 100000
    # 50k input lands on the 40k tier.
    d = decision(
        "gpt-5.6-terra", "vsllm-r", "openai-responses", "max", 50000, cfg
    )
    assert d is not None
    assert d["to"] == "high"
    assert d["tier_floor"] == 40000


def test_cap_decision_empty_ladder_never_caps(decision):
    # [] ladder = explicit opt-out: even 500k input passes xhigh through.
    cfg = _cfg(effort_ladder=())
    assert (
        decision(
            "gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 500000, cfg
        )
        is None
    )


def test_cap_decision_default_config_unchanged(decision):
    # Default config (no tools key) keeps the pre-ladder flat behavior for
    # the scoped model — flat cap at 40k to high, tier_floor mirrors the
    # flat floor — while the SCOPE narrows to terra only (2026-09-07).
    d = decision(
        "gpt-5.6-terra", "vsllm-r", "openai-responses", "xhigh", 42000, _cfg()
    )
    assert d is not None
    assert d["from"] == "xhigh"
    assert d["to"] == "high"
    assert d["tier_floor"] == DEGENERATE_INPUT_TOKEN_FLOOR == 40000


def test_degenerate_effort_telemetry_string_includes_tier_and_model():
    import app.main as main

    d = {
        "from": "xhigh",
        "to": "high",
        "input_tokens": 42000,
        "floor": 40000,
        "tier_floor": 40000,
        "model": "gpt-5.6-terra",
    }
    s = main._degenerate_effort_telemetry(d)
    assert s == "xhigh->high@42000in[tier=40000][model=gpt-5.6-terra]"
    assert "[tier=40000]" in s
    assert "[model=gpt-5.6-terra]" in s


def test_degenerate_effort_telemetry_tolerates_missing_model():
    # Legacy decision dicts (pre-2026-09-07) lack the model key — the
    # telemetry string stays well-formed with an empty model slot.
    import app.main as main

    s = main._degenerate_effort_telemetry(
        {"from": "max", "to": "high", "input_tokens": 63000, "floor": 40000, "tier_floor": 40000}
    )
    assert s == "max->high@63000in[tier=40000][model=]"


# ─────────────────────────────────────────────────────────────────────
# UI chip-state derivation: degenerate_cap_chip_state
# (mirrored in app.js deriveCapChipState)
# ─────────────────────────────────────────────────────────────────────


def test_chip_state_default_terra_on():
    cfg = degenerate_guard_config({})
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-terra") is True
    # Dash-spelled input canonicalizes to the same state.
    assert degenerate_cap_chip_state(cfg, "gpt-5-6-terra") is True


def test_chip_state_default_sol_astra_off():
    cfg = degenerate_guard_config({})
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-sol") is False
    assert degenerate_cap_chip_state(cfg, "gpt-6-astra") is False
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-luna") is False


def test_chip_state_override_enabled_wins():
    cfg = _gcfg(
        model_overrides={
            "gpt-5.6-terra": {"enabled": False},
            "gpt-6-astra": {"enabled": True},
        }
    )
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-terra") is False
    assert degenerate_cap_chip_state(cfg, "gpt-6-astra") is True


def test_chip_state_explicit_models_membership():
    cfg = _gcfg(models=["gpt-5.6-sol"])
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-sol") is True
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-terra") is False


def test_chip_state_empty_models_opt_out():
    cfg = _gcfg(models=[])
    assert degenerate_cap_chip_state(cfg, "gpt-5.6-terra") is False


def test_chip_state_override_without_enabled_falls_back_to_models():
    cfg = _gcfg(model_overrides={"gpt-6-astra": {"effort_cap": "high"}})
    assert degenerate_cap_chip_state(cfg, "gpt-6-astra") is False


def test_chip_state_raw_models_list_canonicalized():
    # The UI reads RAW config (pre-normalization) — dash entries must match.
    assert degenerate_cap_chip_state({"models": ["gpt-5-6-terra"]}, "gpt-5.6-terra") is True


def test_chip_state_never_raises_on_garbage():
    # Fail-open: a non-dict cfg falls back to defaults.
    assert degenerate_cap_chip_state(None, "gpt-5.6-terra") is True
    assert degenerate_cap_chip_state("junk", "gpt-5.6-sol") is False


# ─────────────────────────────────────────────────────────────────────
# Observability: note_degenerate_output + degenerate END flag
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "_USAGE_LOG_PATH", str(tmp_path / "usage_stats.jsonl"))
    monkeypatch.setattr(obs, "_CONSOLE_LOG_PATH", str(tmp_path / "console_logs.jsonl"))
    obs.console_logs.clear()
    obs.usage_stats.clear()
    if hasattr(obs, "_terminal_end_registry"):
        obs._terminal_end_registry.clear()
    yield
    obs.console_logs.clear()
    obs.usage_stats.clear()
    if hasattr(obs, "_terminal_end_registry"):
        obs._terminal_end_registry.clear()


def test_note_degenerate_output_writes_console_entry():
    obs.note_degenerate_output(
        "vsllm-r",
        "gpt-6-astra",
        in_tokens=52000,
        out_tokens=42,
        request_id="req_dog1",
        effort="xhigh",
        action="telemetry",
    )

    dog_events = [e for e in obs.console_logs if e.get("event") == "degenerate_output"]
    assert len(dog_events) == 1
    e = dog_events[0]
    assert e["provider"] == "vsllm-r"
    assert e["model"] == "gpt-6-astra"
    assert e["in_tokens"] == 52000
    assert e["out_tokens"] == 42
    assert e["effort"] == "xhigh"
    assert e["action"] == "telemetry"
    assert e["request_id"] == "req_dog1"


def test_note_degenerate_output_minimal_fields():
    obs.note_degenerate_output("vsllm-r", "gpt-5.5", 40000, 0)
    dog_events = [e for e in obs.console_logs if e.get("event") == "degenerate_output"]
    assert len(dog_events) == 1
    assert "request_id" not in dog_events[0]
    assert "effort" not in dog_events[0]


def test_note_degenerate_output_is_fail_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(obs, "_persist_entry", _boom)
    # Must not raise — telemetry can never take down routing.
    obs.note_degenerate_output("vsllm-r", "gpt-6-astra", 50000, 10)


def test_log_request_degenerate_flag_on_end_row():
    obs.log_request(
        provider="vsllm-r",
        model="gpt-6-astra",
        status=200,
        ttft=1.2,
        in_tokens=52000,
        out_tokens=42,
        cached_tokens=0,
        config={},
        total_time=3.0,
        request_id="req_dog2",
        client="openai",
        stream=True,
        degenerate=True,
    )

    ends = [e for e in obs.console_logs if e.get("event") == "end"]
    assert len(ends) == 1
    assert ends[0]["degenerate"] is True
    # Telemetry-only: status stays 200, no error token, usage row healthy.
    assert ends[0]["status"] == 200
    assert "error" not in ends[0]


def test_log_request_without_degenerate_flag_stays_clean():
    obs.log_request(
        provider="vsllm-r",
        model="gpt-6-astra",
        status=200,
        ttft=1.2,
        in_tokens=52000,
        out_tokens=2000,
        cached_tokens=0,
        config={},
        total_time=3.0,
        request_id="req_dog3",
        client="openai",
        stream=True,
    )

    ends = [e for e in obs.console_logs if e.get("event") == "end"]
    assert len(ends) == 1
    assert "degenerate" not in ends[0]
