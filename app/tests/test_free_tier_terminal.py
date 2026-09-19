"""Terminal-gate regression tests for the OpenCode Zen free-tier 403 policy.

OpenCode Zen free-tier models (muse-spark-1.3-contributor-free,
mimo-v2.5-free) return HTTP 403 `FreeTierError: OpenCode's free tier can
only be used from within OpenCode` when dialed outside the OpenCode app.
Forensic probes v1-v4 (2026-09-18) proved the wall is server-side and
app-only — no header/UA/auth/project/transport variant bypasses it — so
retrying the leaf can never succeed. Pre-fix behavior: the 403 classified
as `auth` (90s ephemeral softban), the combo advanced through siblings,
and once the chain exhausted, the never-stop wrap re-dialed the leaf after
every ban expiry — an infinite loop burning wall-clock and quota.

The fix (app/error_prevention.py + app/main.py, 2026-09-18):
  - classifies the 401/403 as a NEW `free_tier` error type BEFORE pattern
    matching (the 'auth' pattern list contains '403'/'forbidden' and would
    otherwise win),
  - benches the leaf with an immediate 60-minute longban (ephemeral:
    sidecar-persisted, config.yaml is never rewritten),
  - gates the never-stop combo WRAP when EVERY leaf in the active chain
    sits under a live free-tier ban. Mixed chains keep never-stop exactly
    as before; single-failure ADVANCE to healthy siblings is untouched —
    403 stays in _RECOVERABLE.

Pure unit tests — no live sockets, no config.yaml writes, no disk I/O.
Mirrors test_hcnsec_plan_limit_softban.py (EP unit-test style) and
test_recoverable_status_expansion.py (app.main import pattern).
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.error_prevention as ep
import app.main as main
from app.error_prevention import ErrorPreventionManager, check_ban


FREE_TIER_MSG = (
    "FreeTierError: OpenCode's free tier can only be used from within OpenCode"
)

# Live router combo-chain leaf shape: 3-tuple (model, provider, extra).
LEAF_A = ("muse-spark-1.3-contributor-free", "zen", None)
LEAF_B = ("mimo-v2.5-free", "zen", None)


def _enabled_config():
    return {
        "error_prevention": {"enabled": True, "consecutive_threshold": 3},
        # minimal providers shape so manually_enable_model-style swaps don't blow up
        "providers": {},
    }


def _live_free_tier_entry(provider, model, seconds=3600, ban_state="longban"):
    """A LIVE free-tier ban entry (ban_state set, ban_until in the future)."""
    return {
        "streak": 0,
        "last_error_time": time.time(),
        "ban_state": ban_state,
        "ban_until": time.time() + seconds,
        "ban_escalation_count": 1,
        "error_type": "free_tier",
        "provider": provider,
        "model": model,
    }


def _cfg_state(state):
    cfg = _enabled_config()
    cfg["error_prevention_state"] = state
    return cfg


def _all_banned_cfg():
    return _cfg_state({
        "zen/muse-spark-1.3-contributor-free/free_tier":
            _live_free_tier_entry("zen", "muse-spark-1.3-contributor-free"),
    })


# ── Scenarios 1-4: classify_error ──────────────────────────────────────

class TestClassifyFreeTier:
    """The free-tier 403 must classify free_tier; the status gate must not
    swallow rate limits or 5xx that merely mention 'free tier'."""

    def test_real_zen_body_classifies_free_tier(self):
        mgr = ErrorPreventionManager(_enabled_config())
        assert mgr.classify_error(403, FREE_TIER_MSG) == "free_tier"

    def test_401_variant_classifies_free_tier(self):
        mgr = ErrorPreventionManager(_enabled_config())
        assert mgr.classify_error(401, FREE_TIER_MSG) == "free_tier"

    def test_403_forbidden_still_auth(self):
        """Regression guard: plain 403 must keep the auth classification."""
        mgr = ErrorPreventionManager(_enabled_config())
        assert mgr.classify_error(403, "forbidden") == "auth"

    def test_429_free_tier_quota_stays_rate_limit(self):
        """Status-gate guard: 429 quota mentions are rate limits, not the wall."""
        mgr = ErrorPreventionManager(_enabled_config())
        assert mgr.classify_error(429, "free tier quota exceeded") == "rate_limit"

    def test_500_freetier_stays_server_error(self):
        """Status-gate guard: 5xx free-tier mentions stay server errors."""
        mgr = ErrorPreventionManager(_enabled_config())
        assert mgr.classify_error(500, "freetier") == "server_error"


# ── Scenario 5: is_free_tier_error predicate ──────────────────────────

class TestIsFreeTierError:
    def test_none_message(self):
        assert not ep.is_free_tier_error(403, None)

    def test_empty_message(self):
        assert not ep.is_free_tier_error(403, "")

    def test_none_status(self):
        assert not ep.is_free_tier_error(None, FREE_TIER_MSG)

    def test_status_200(self):
        assert not ep.is_free_tier_error(200, "freetier")

    def test_403_hits_each_marker(self):
        for marker in ep.FREE_TIER_MARKERS:
            assert ep.is_free_tier_error(403, marker), marker

    def test_case_insensitive(self):
        # The live upstream body mixes case: 'FreeTierError ... free tier ...'
        assert ep.is_free_tier_error(403, FREE_TIER_MSG)

    def test_401_also_gated(self):
        assert ep.is_free_tier_error(401, "free tier")


# ── Scenario 6: record_error immediate longban ────────────────────────

class TestRecordErrorFreeTier:
    """First free-tier 403 -> immediate 60m longban, ephemeral action."""

    def test_immediate_longban_and_action_shape(self):
        cfg = _enabled_config()
        mgr = ErrorPreventionManager(cfg)
        before = time.time()
        action = mgr.record_error(
            "zen", "muse-spark-1.3-contributor-free", 403, FREE_TIER_MSG
        )
        after = time.time()

        assert action is not None
        assert action["action"] == "free_tier"
        assert action["error_type"] == "free_tier"
        assert action["ephemeral"] is True
        assert action["duration_seconds"] == 3600
        assert action["duration_minutes"] == 60
        assert action["notify"] is False

        entry = cfg["error_prevention_state"][
            "zen/muse-spark-1.3-contributor-free/free_tier"
        ]
        assert entry["ban_state"] == "longban"
        # ban_until ~= now+3600, tolerance ±60s for slow clocks in CI
        assert before + 3600 - 60 <= entry["ban_until"] <= after + 3600 + 60
        assert entry["ban_escalation_count"] == 1

    def test_ban_surfaces_in_combo_prefilter(self):
        """check_ban must report the leaf benched so resolve_combo /
        advance_combo_retry skip it for the next ~60 minutes."""
        cfg = _enabled_config()
        mgr = ErrorPreventionManager(cfg)
        mgr.record_error(
            "zen", "muse-spark-1.3-contributor-free", 403, FREE_TIER_MSG
        )

        banned, ban_state, remaining = check_ban(
            cfg, "zen", "muse-spark-1.3-contributor-free"
        )
        assert banned is True
        assert ban_state == "longban"
        assert remaining is not None and 3500 < remaining <= 3600

    def test_admin_clear_temp_bans_lifts_it(self):
        """longban is a timed ban: clear_temp_bans_with_count (admin API)
        must lift it without a restart."""
        cfg = _enabled_config()
        mgr = ErrorPreventionManager(cfg)
        mgr.record_error(
            "zen", "muse-spark-1.3-contributor-free", 403, FREE_TIER_MSG
        )
        assert mgr.clear_temp_bans_with_count() == 1
        banned, _, _ = check_ban(cfg, "zen", "muse-spark-1.3-contributor-free")
        assert banned is False


# ── Scenario 7: chain_all_free_tier_banned truth table ─────────────────

class TestChainAllFreeTierBanned:
    def test_empty_chain_false(self):
        cfg = _all_banned_cfg()
        assert not ep.chain_all_free_tier_banned(cfg, [])

    def test_none_chain_false(self):
        cfg = _all_banned_cfg()
        assert not ep.chain_all_free_tier_banned(cfg, None)

    def test_one_unbanned_leaf_false(self):
        cfg = _all_banned_cfg()
        # LEAF_B has no state entry at all
        assert not ep.chain_all_free_tier_banned(cfg, [LEAF_B])

    def test_all_leaves_banned_true(self):
        cfg = _cfg_state({
            "zen/muse-spark-1.3-contributor-free/free_tier":
                _live_free_tier_entry("zen", "muse-spark-1.3-contributor-free"),
            "zen/mimo-v2.5-free/free_tier":
                _live_free_tier_entry("zen", "mimo-v2.5-free"),
        })
        assert ep.chain_all_free_tier_banned(cfg, [LEAF_A, LEAF_B])

    def test_auth_only_ban_false(self):
        """A leaf benched for a DIFFERENT reason (live auth ban) must not
        count — that ban expires soon and the leaf is worth retrying, so
        never-stop still applies."""
        cfg = _cfg_state({
            "zen/muse-spark-1.3-contributor-free/auth": {
                "streak": 0,
                "last_error_time": time.time(),
                "ban_state": "softban",
                "ban_until": time.time() + 90,
                "ban_escalation_count": 1,
                "error_type": "auth",
                "provider": "zen",
                "model": "muse-spark-1.3-contributor-free",
            },
        })
        assert not ep.chain_all_free_tier_banned(cfg, [LEAF_A])

    def test_expired_free_tier_ban_false(self):
        cfg = _cfg_state({
            "zen/muse-spark-1.3-contributor-free/free_tier":
                _live_free_tier_entry(
                    "zen", "muse-spark-1.3-contributor-free", seconds=-10
                ),
        })
        assert not ep.chain_all_free_tier_banned(cfg, [LEAF_A])

    def test_unresolvable_leaf_shapes_false(self):
        cfg = _all_banned_cfg()
        for bad in (None, 42, ("only-model",), {"nope": 1}, object()):
            assert not ep.chain_all_free_tier_banned(cfg, [bad])

    def test_none_config_false(self):
        assert not ep.chain_all_free_tier_banned(None, [LEAF_A])

    def test_empty_state_false(self):
        cfg = _enabled_config()
        assert not ep.chain_all_free_tier_banned(cfg, [LEAF_A])

    def test_dict_and_string_leaf_shapes(self):
        """Tolerance: dict and 'provider/model' string leaves resolve too."""
        cfg = _cfg_state({
            "zen/dict-model/free_tier":
                _live_free_tier_entry("zen", "dict-model"),
            "zen/string-model/free_tier":
                _live_free_tier_entry("zen", "string-model"),
        })
        chain = [
            {"provider": "zen", "model": "dict-model"},
            "zen/string-model",
        ]
        assert ep.chain_all_free_tier_banned(cfg, chain)

    def test_case_insensitive_lookup(self):
        """State keys are lowercase; mixed-case chain leaves must resolve."""
        cfg = _all_banned_cfg()
        chain = [("Muse-Spark-1.3-Contributor-Free", "Zen", None)]
        assert ep.chain_all_free_tier_banned(cfg, chain)


# ── Scenario 8: _raise_combo_wrap terminal gate ────────────────────────

class TestRaiseComboWrapTerminalGate:
    """The never-stop wrap must die ONLY when every leaf is free-tier
    benched AND the last error is the policy 403."""

    def test_all_banned_403_returns_none_no_raise(self):
        cfg = _all_banned_cfg()
        result = main._raise_combo_wrap(
            "muse-spark-1.3-contributor-free", cfg, None,
            {"pass_no": 1}, [LEAF_A], "muse-spark-1.3-contributor-free",
            None, 403, FREE_TIER_MSG, "exhausted",
        )
        assert result is None

    def test_unbanned_chain_still_raises_never_stop(self):
        cfg = _enabled_config()  # no bans at all
        with pytest.raises(main._ComboFallbackNeeded) as exc_info:
            main._raise_combo_wrap(
                "muse-spark-1.3-contributor-free", cfg, None,
                {"pass_no": 1}, [LEAF_A], "muse-spark-1.3-contributor-free",
                None, 403, FREE_TIER_MSG, "exhausted",
            )
        wrap = exc_info.value.retry_state
        assert wrap["pass_no"] == 2
        assert "terminal_free_tier" not in wrap

    def test_mixed_chain_still_raises_never_stop(self):
        """One benched leaf + one healthy sibling -> never-stop preserved."""
        cfg = _all_banned_cfg()
        with pytest.raises(main._ComboFallbackNeeded):
            main._raise_combo_wrap(
                "muse-spark-1.3-contributor-free", cfg, None,
                {"pass_no": 1}, [LEAF_A, LEAF_B],
                "muse-spark-1.3-contributor-free",
                None, 403, FREE_TIER_MSG, "exhausted",
            )

    def test_all_banned_non_403_marks_terminal_wrap(self):
        """Entry/all-banned path: last error NOT the policy 403 (synthetic
        reason) — the restart gate still returns the marked wrap (0.0s
        backoff) so the entry gate answers the client with the 403."""
        cfg = _all_banned_cfg()
        with pytest.raises(main._ComboFallbackNeeded) as exc_info:
            main._raise_combo_wrap(
                "muse-spark-1.3-contributor-free", cfg, None,
                {"pass_no": 3}, [LEAF_A], "muse-spark-1.3-contributor-free",
                None, 500, "internal server error", "exhausted",
            )
        assert exc_info.value.backoff == 0.0
        assert exc_info.value.retry_state.get("terminal_free_tier") is True

    def test_knob_off_returns_none_regardless(self):
        """combo_infinite_retry=False keeps the pre-existing no-raise path
        even without any free-tier state."""
        cfg = _enabled_config()
        cfg["settings"] = {"combo_infinite_retry": False}
        result = main._raise_combo_wrap(
            "muse-spark-1.3-contributor-free", cfg, None,
            {"pass_no": 1}, [LEAF_A], "muse-spark-1.3-contributor-free",
            None, 500, "boom", "exhausted",
        )
        assert result is None


# ── Scenarios 9-10: _combo_restart_or_give_up ─────────────────────────

class TestComboRestartOrGiveUp:
    """Terminal wrap marker (entry/all-banned) vs unchanged normal wrap."""

    def test_all_banned_with_config_returns_terminal_wrap(self):
        cfg = _all_banned_cfg()
        backoff, wrap = main._combo_restart_or_give_up(
            "muse-spark-1.3-contributor-free", {"pass_no": 2}, [LEAF_A],
            "muse-spark-1.3-contributor-free", None, None, "exhausted",
            config=cfg,
        )
        assert backoff == 0.0
        assert wrap["terminal_free_tier"] is True
        assert wrap["chain"] == [LEAF_A]
        assert wrap["idx"] == 0
        assert wrap["pass_no"] == 3
        assert wrap["deadline"] is None
        assert wrap["original_model"] == "muse-spark-1.3-contributor-free"
        assert wrap["cache_bp"] is None

    def test_config_none_default_keeps_normal_wrap(self):
        backoff, wrap = main._combo_restart_or_give_up(
            "muse-spark-1.3-contributor-free", {"pass_no": 1}, [LEAF_A],
            "muse-spark-1.3-contributor-free", None, None, "exhausted",
        )
        assert backoff == 2.0
        assert "terminal_free_tier" not in wrap
        assert wrap["chain"] == [LEAF_A]
        assert wrap["pass_no"] == 2
        assert wrap["idx"] == 0

    def test_config_with_unbanned_leaf_keeps_normal_wrap(self):
        """config present but NOT all-banned -> the gate safely no-ops."""
        cfg = _enabled_config()
        backoff, wrap = main._combo_restart_or_give_up(
            "muse-spark-1.3-contributor-free", {"pass_no": 1}, [LEAF_A],
            "muse-spark-1.3-contributor-free", None, None, "exhausted",
            config=cfg,
        )
        assert backoff == 2.0
        assert "terminal_free_tier" not in wrap

    def test_backoff_ladder_unchanged(self):
        """2,4,8,16,30,30... — the never-stop backoff curve is untouched."""
        for pass_no, expected in ((1, 2.0), (2, 4.0), (3, 8.0),
                                  (4, 16.0), (5, 30.0), (6, 30.0)):
            backoff, wrap = main._combo_restart_or_give_up(
                "m", {"pass_no": pass_no}, [LEAF_A], "m",
                None, None, "exhausted",
            )
            assert backoff == expected
            assert "terminal_free_tier" not in wrap

    def test_terminal_wrap_with_none_retry_state(self):
        """Defensive: retry_state=None must not blow up the marker path."""
        cfg = _all_banned_cfg()
        backoff, wrap = main._combo_restart_or_give_up(
            "muse-spark-1.3-contributor-free", None, [LEAF_A],
            "muse-spark-1.3-contributor-free", None, None, "exhausted",
            config=cfg,
        )
        assert backoff == 0.0
        assert wrap["terminal_free_tier"] is True
        assert wrap["pass_no"] == 2
        assert wrap["chain"] == [LEAF_A]

    def test_normal_path_resets_wall_start(self):
        """The normal (non-terminal) wrap must keep clearing
        request.state.bsl_chain_wall_start so the next pass re-arms the
        chain budget (pre-existing behavior, unchanged)."""
        class _NS: pass
        req = _NS()
        req.state = _NS()
        req.state.bsl_chain_wall_start = 123.456
        main._combo_restart_or_give_up(
            "m", {"pass_no": 1}, [LEAF_A], "m", None, req, "exhausted",
        )
        assert req.state.bsl_chain_wall_start is None


# ── Spec invariants (NON-NEGOTIABLE) ───────────────────────────────────

def test_recoverable_still_contains_403():
    """403 MUST stay in _RECOVERABLE: single-failure ADVANCE to healthy
    siblings is correct combo behavior; only the WRAP is gated.
    (test_recoverable_status_expansion.py is the full guard.)"""
    assert 403 in main._RECOVERABLE


def test_error_types_registry_has_free_tier():
    """/api endpoints and stats surfaces that walk ERROR_TYPES stay aware
    of the new type."""
    assert "free_tier" in ErrorPreventionManager.ERROR_TYPES
    assert ErrorPreventionManager.ERROR_TYPES["free_tier"] == []
