"""State-key normalization + case-variant merge (2026-09-18).

Upstreams return the same model under different casing, so error-streak state
keyed on the raw model id split into two independent entries. Two consequences:

  1. A model could be half-banned: failures recorded under 'GLM-5.2' were
     invisible to a lookup using 'glm-5.2'.
  2. /api/config emitted both keys in one JSON object. Strict parsers reject
     duplicate keys outright (PowerShell ConvertFrom-Json already did).

Live config.yaml carried 3 model ids in mixed casing when this was found:
GLM-5.2, Kimi-K2.6, DeepSeek-V4-Pro.
"""

import time

import pytest

from app.error_prevention import ErrorPreventionManager


def _mgr(state=None, **settings):
    cfg = {
        "error_prevention": {"enabled": True, **settings},
        "error_prevention_state": state if state is not None else {},
    }
    return ErrorPreventionManager(cfg)


# ── Key construction ────────────────────────────────────────────────────────


def test_get_state_key_is_case_insensitive():
    m = _mgr()
    assert m.get_state_key("X5M5X", "GLM-5.2", "rate_limit") == m.get_state_key(
        "x5m5x", "glm-5.2", "rate_limit"
    )


def test_get_state_key_lowercases_both_segments():
    m = _mgr()
    assert m.get_state_key("X5M5X", "GLM-5.2", "rate_limit") == "x5m5x/glm-5.2/rate_limit"


def test_error_type_is_not_mangled_by_normalization():
    """error_type contains an underscore and must survive verbatim."""
    m = _mgr()
    assert m.get_state_key("p", "m", "rate_limit").endswith("/rate_limit")
    assert m.get_state_key("p", "m", "server_error").endswith("/server_error")


def test_slashed_model_id_round_trips():
    """Model ids like 'deepseek/deepseek-v4-pro' contain a slash themselves."""
    m = _mgr()
    key = m.get_state_key("CommandCode", "DeepSeek/DeepSeek-V4-Pro", "timeout")
    assert key == "commandcode/deepseek/deepseek-v4-pro/timeout"


# ── Lookups match regardless of casing ──────────────────────────────────────


def test_failure_under_one_casing_is_visible_to_the_other():
    """The half-ban bug: record as 'GLM-5.2', look up as 'glm-5.2'."""
    m = _mgr(rate_limit_cooldown_seconds=90)
    for _ in range(5):
        m.record_error("x5m5x", "GLM-5.2", 429, "rate limit exceeded")

    banned, _kind, _remaining = m.is_banned("x5m5x", "glm-5.2")
    assert banned is True, "ban recorded under upper-case casing must be visible"


def test_record_success_clears_streak_across_casing():
    m = _mgr()
    for _ in range(3):
        m.record_error("prov", "Model-A", 500, "internal server error")
    m.record_success("prov", "model-a")

    assert not any(
        (e or {}).get("streak") for e in m.state.values()
    ), "success under a different casing must clear the streak"


def test_state_holds_one_entry_for_two_casings():
    """The core fix: two casings must not create two independent streaks."""
    m = _mgr()
    m.record_error("prov", "Model-A", 500, "internal server error")
    m.record_error("prov", "model-a", 500, "internal server error")

    server_keys = [k for k in m.state if k.endswith("/server_error")]
    assert len(server_keys) == 1, f"expected a single merged key, got {server_keys}"
    assert server_keys[0] == "prov/model-a/server_error"


# ── Load-time merge of pre-existing state ───────────────────────────────────


def test_merge_collapses_case_variant_keys_on_load():
    state = {
        "x5m5x/glm-5.2/rate_limit": {"streak": 1},
        "x5m5x/GLM-5.2/rate_limit": {"streak": 4},
    }
    m = _mgr(state)
    assert len(m.state) == 1
    assert "x5m5x/glm-5.2/rate_limit" in m.state
    assert m.state["x5m5x/glm-5.2/rate_limit"]["streak"] == 4


def test_merge_keeps_max_counters():
    """Real schema fields: streak and ban_escalation_count."""
    state = {
        "p/m/timeout": {"streak": 2, "ban_escalation_count": 3},
        "p/M/timeout": {"streak": 5, "ban_escalation_count": 1},
    }
    m = _mgr(state)
    entry = m.state["p/m/timeout"]
    assert entry["streak"] == 5
    assert entry["ban_escalation_count"] == 3


def test_merge_keeps_latest_timestamps():
    state = {
        "p/m/timeout": {"last_error_time": 100.0, "ban_until": 500.0},
        "p/M/timeout": {"last_error_time": 900.0, "ban_until": 200.0},
    }
    m = _mgr(state)
    entry = m.state["p/m/timeout"]
    assert entry["last_error_time"] == 900.0
    assert entry["ban_until"] == 500.0, "a live ban must never be shortened"


def test_merge_prefers_the_more_restrictive_ban_state():
    """Losing a ban is worse than holding one slightly too long."""
    state = {
        "p/m/auth": {"ban_state": None},
        "p/M/auth": {"ban_state": "disabled"},
    }
    m = _mgr(state)
    assert m.state["p/m/auth"]["ban_state"] == "disabled"


def test_merge_disabled_outranks_timed_ban():
    state = {
        "p/m/auth": {"ban_state": "disabled"},
        "p/M/auth": {"ban_state": "softban"},
    }
    m = _mgr(state)
    assert m.state["p/m/auth"]["ban_state"] == "disabled"


def test_merge_renames_lone_uppercase_entry():
    """A single mixed-case entry with no twin is renamed, not dropped."""
    state = {"p/Model-X/timeout": {"streak": 3}}
    m = _mgr(state)
    assert "p/model-x/timeout" in m.state
    assert m.state["p/model-x/timeout"]["streak"] == 3


def test_merged_state_survives_a_real_ban_lookup():
    """End-to-end: split on-disk state merges into one live ban."""
    future = time.time() + 600
    state = {
        "x5m5x/glm-5.2/rate_limit": {"ban_state": None},
        "x5m5x/GLM-5.2/rate_limit": {"ban_state": "softban", "ban_until": future},
    }
    m = _mgr(state)
    banned, kind, remaining = m.is_banned("x5m5x", "GLM-5.2")
    assert banned is True
    assert kind == "softban"
    assert remaining is not None and remaining > 0


def test_no_duplicate_keys_after_merge():
    """The JSON-parser crash: no two keys may differ only by case."""
    state = {
        "x5m5x/glm-5.2/rate_limit": {},
        "x5m5x/GLM-5.2/rate_limit": {},
        "x5m5x/Kimi-K2.6/timeout": {},
        "x5m5x/kimi-k2.6/timeout": {},
    }
    m = _mgr(state)
    lowered = [k.lower() for k in m.state]
    assert len(lowered) == len(set(lowered))


# ── Safety ──────────────────────────────────────────────────────────────────


def test_empty_state_is_untouched():
    m = _mgr({})
    assert m.state == {}


def test_already_normalized_state_is_unchanged():
    state = {
        "p/m/timeout": {"streak": 1},
        "p/other/auth": {"streak": 2},
    }
    m = _mgr(dict(state))
    assert m.state == state


def test_non_string_keys_do_not_crash_the_merge():
    m = _mgr({7: {"streak": 1}, "p/M/timeout": {}})
    assert 7 in m.state


def test_malformed_entries_do_not_crash_the_merge():
    """Corrupt values must not take the router down on boot."""
    state = {
        "p/m/timeout": {"streak": "not-a-number"},
        "p/M/timeout": {"streak": 3},
    }
    m = _mgr(state)
    assert len(m.state) == 1


def test_merge_is_idempotent():
    state = {
        "p/m/timeout": {"streak": 2},
        "p/M/timeout": {"streak": 5},
    }
    first = _mgr(state)
    snapshot = dict(first.state)
    second = ErrorPreventionManager(
        {"error_prevention": {"enabled": True}, "error_prevention_state": first.state}
    )
    assert second.state == snapshot
