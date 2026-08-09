"""
Tests for bsl_agentic_ultra_router balanced routing.

Covers Scout-first classification, deterministic role selection, transport-only
fallbacks, ignored consult_routes, always-on behavior, depth, and global fallback.
"""

from app.models import ChatCompletionRequest, Message
from app.middleware.bsl_agentic_ultra_router import (
    route_bsl_agentic_ultra,
    _get_bsl_agentic_ultra_cfg,
    AGENTIC_ULTRA_DEPTH,
)


def _request(text: str, model: str = "blacksand-agentic-ultra") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=text)],
    )


def _config(enabled: bool = True, router_enabled: bool = True) -> dict:
    return {
        "tools": {"bsl_agentic_ultra_router": router_enabled},
        "bsl_models": {
            "bsl_agentic_ultra": {
                "enabled": enabled,
                "agent_routes": {
                    "scout": {"primary": "coder-1"},
                    "power_coder": {"primary": "coder-2", "fallback_1": "pf1"},
                    "ultra_coder": {"primary": "coder-3", "fallback_1": "uf1"},
                },
                "global_last_fallback": "GLM-5.2",
            },
        },
    }


# ─── Config reader ───────────────────────────────────────────────────────────


def test_canonical_config_read() -> None:
    assert _get_bsl_agentic_ultra_cfg(_config())["enabled"] is True


def test_legacy_config_read() -> None:
    cfg = {"bsl_agentic_ultra": {"enabled": True, "agent_routes": {}}}
    assert _get_bsl_agentic_ultra_cfg(cfg)["enabled"] is True


def test_get_cfg_none() -> None:
    assert _get_bsl_agentic_ultra_cfg(None) == {}


# ─── Always-on routing ────────────────────────────────────────────────────────


def test_empty_config_unresolved() -> None:
    d = route_bsl_agentic_ultra(_request("hello"), {})
    assert d.selected_model == ""
    assert d.source == "unresolved"


def test_router_flag_off_still_routes() -> None:
    d = route_bsl_agentic_ultra(_request("implement"), _config(router_enabled=False))
    assert d.source != "disabled_default"
    assert d.selected_model != ""


def test_enabled_flag_false_still_routes() -> None:
    d = route_bsl_agentic_ultra(_request("implement"), _config(enabled=False))
    assert d.source != "disabled_default"
    assert d.selected_model != ""


def test_balanced_does_not_use_consult_matrix() -> None:
    cfg = _config()
    cfg["bsl_models"]["bsl_agentic_ultra"]["consult_routes"] = {
        "power_coder": {"primary": "consult-power"},
    }
    text = "implement and refactor the authentication module with tests"
    d = route_bsl_agentic_ultra(_request(text), cfg)
    assert d.category == "refactor"
    assert d.selected_model == "coder-1"
    assert d.source == "scout_direct"
    assert d.consulted is False
    assert "consult-power" not in d.fallback_chain


def test_scout_answers_trivial() -> None:
    d = route_bsl_agentic_ultra(_request("what is your name?"), _config())
    assert d.source == "scout_direct"
    assert d.consulted is False
    assert d.selected_model == "coder-1"


def test_global_last_fallback() -> None:
    cfg = {
        "tools": {"bsl_agentic_ultra_router": True},
        "bsl_models": {
            "bsl_agentic_ultra": {
                "enabled": True,
                "agent_routes": {},
                "global_last_fallback": "GLM-5.2",
            }
        },
    }
    d = route_bsl_agentic_ultra(_request("do something"), cfg)
    assert d.selected_model == "GLM-5.2"


def test_depth_always_balanced() -> None:
    d = route_bsl_agentic_ultra(_request("implement a function"), _config())
    assert d.depth == "balanced"
    assert AGENTIC_ULTRA_DEPTH == "balanced"


def test_balanced_plan_is_one_member_and_admitted() -> None:
    from app.middleware.bsl_orchestrator_engine import build_balanced_plan

    plan = build_balanced_plan("implement auth", "power_coder")
    assert len(plan.state.phases) == 1
    assert plan.state.effort_tier == "balanced"
    assert plan.state.phases[0].sub_role == "power_coder"


def test_balanced_phase_success_is_deterministic_done() -> None:
    from app.middleware.bsl_orchestrator_engine import build_balanced_plan, finish_phase

    state = build_balanced_plan("implement auth", "power_coder").state
    decision = finish_phase(state, summary="completed", model="coder-2")
    assert decision.action == "done"
    assert decision.source == "deterministic"
    assert state.done is True


def test_balanced_phase_ambiguity_requires_scout() -> None:
    from app.middleware.bsl_orchestrator_engine import (
        AmbiguousPhase,
        build_balanced_plan,
        finish_phase,
    )

    state = build_balanced_plan("implement auth", "power_coder").state
    try:
        finish_phase(state, status="success", gaps=["missing tests"])
    except AmbiguousPhase as exc:
        assert str(exc) == "Scout reassessment required"
    else:
        raise AssertionError("ambiguous phase must require Scout reassessment")
