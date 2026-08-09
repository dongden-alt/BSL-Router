"""
Tests for bsl_agentic_router (fast-tier agentic coding router, depth=fast LOCKED).

Covers:
- Canonical bsl_models.bsl_agentic config path
- Legacy bsl_agentic config path
- Safe default OFF (missing keys = disabled)
- Agent route selection with 3-slot fallback chains
- Scout fallback when classified agent has no route
- global_last_fallback as final safety net
- default_route override bypasses matrix
- Depth is always "fast"
- fail_open on unexpected errors
"""

from app.models import ChatCompletionRequest, Message
from app.middleware.bsl_agentic_router import (
    route_bsl_agentic,
    _get_bsl_agentic_cfg,
    AGENTIC_DEPTH,
)


def _request(text: str, model: str = "blacksand-agentic") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=text)],
    )


def _config(enabled: bool = True, router_enabled: bool = True, glf: str = "GLM-5.2") -> dict:
    return {
        "tools": {"bsl_agentic_router": router_enabled},
        "bsl_models": {
            "bsl_agentic": {
                "enabled": enabled,
                "agent_routes": {
                    "scout": {"primary": "coder-1", "fallback_1": "f1", "fallback_2": "f2"},
                    "power_coder": {"primary": "coder-2", "fallback_1": "pf1"},
                },
                "global_last_fallback": glf,
            },
        },
    }


# ─── Config reader ───────────────────────────────────────────────────────────


def test_canonical_config_read() -> None:
    cfg = _config()
    bsl_cfg = _get_bsl_agentic_cfg(cfg)
    assert bsl_cfg["enabled"] is True
    assert "agent_routes" in bsl_cfg


def test_legacy_config_read() -> None:
    cfg = {"bsl_agentic": {"enabled": True, "agent_routes": {"scout": "coder-1"}}}
    bsl_cfg = _get_bsl_agentic_cfg(cfg)
    assert bsl_cfg["enabled"] is True


def test_canonical_preferred_over_legacy() -> None:
    cfg = {
        "bsl_models": {"bsl_agentic": {"enabled": True, "agent_routes": {"scout": "canonical"}}},
        "bsl_agentic": {"enabled": True, "agent_routes": {"scout": "legacy"}},
    }
    bsl_cfg = _get_bsl_agentic_cfg(cfg)
    assert bsl_cfg["agent_routes"]["scout"] == "canonical"


def test_get_cfg_none() -> None:
    assert _get_bsl_agentic_cfg(None) == {}


def test_get_cfg_empty() -> None:
    assert _get_bsl_agentic_cfg({}) == {}


# ─── Always-on routing (gate removal, 2026-08-06) ───


def test_empty_config_unresolved() -> None:
    d = route_bsl_agentic(_request("hello"), {})
    assert d.selected_model == ""
    assert d.source == "unresolved"


def test_none_config_unresolved() -> None:
    d = route_bsl_agentic(_request("hello"), None)  # type: ignore
    assert d.selected_model == ""
    assert d.source == "unresolved"


def test_router_flag_off_still_routes() -> None:
    """Always on: tools flag off does not block routing; matrix resolves."""
    cfg = _config(router_enabled=False)
    d = route_bsl_agentic(_request("implement a function"), cfg)
    assert d.source != "disabled_default"
    assert d.selected_model != ""


# ─── Routing ────────────────────────────────────────────────────────────────


def test_power_coder_route_with_fallbacks() -> None:
    cfg = _config()
    d = route_bsl_agentic(_request("implement a new function to parse files"), cfg)
    assert d.category == "power_coder"
    assert d.selected_model == "coder-2"
    assert "pf1" in d.fallback_chain
    # scout general fallback appended
    assert "coder-1" in d.fallback_chain


def test_scout_fallback_route() -> None:
    cfg = _config()
    d = route_bsl_agentic(_request("search for the config file"), cfg)
    assert d.category == "scout"
    assert d.selected_model == "coder-1"
    assert d.fallback_chain[:2] == ["f1", "f2"]


def test_global_last_fallback_when_no_routes() -> None:
    cfg = {
        "tools": {"bsl_agentic_router": True},
        "bsl_models": {"bsl_agentic": {"enabled": True, "agent_routes": {}, "global_last_fallback": "GLM-5.2"}},
    }
    d = route_bsl_agentic(_request("do something"), cfg)
    assert d.selected_model == "GLM-5.2"
    assert d.source == "global_last_fallback"


def test_default_route_bypasses_matrix() -> None:
    cfg = _config()
    cfg["bsl_models"]["bsl_agentic"]["default_route_enabled"] = True
    cfg["bsl_models"]["bsl_agentic"]["default_route"] = "override-model"
    d = route_bsl_agentic(_request("implement a function"), cfg)
    assert d.selected_model == "override-model"
    assert d.source == "default_route"


def test_depth_always_fast() -> None:
    cfg = _config()
    d = route_bsl_agentic(_request("implement a function"), cfg)
    assert d.depth == "fast"
    assert AGENTIC_DEPTH == "fast"


def test_3slot_dict_route_extraction() -> None:
    cfg = _config()
    cfg["bsl_models"]["bsl_agentic"]["agent_routes"]["planner"] = {
        "primary": "p",
        "fallback_1": "x",
        "fallback_2": "y",
    }
    d = route_bsl_agentic(_request("plan the architecture"), cfg)
    assert d.category == "planner"
    assert d.selected_model == "p"
    assert "x" in d.fallback_chain
    assert "y" in d.fallback_chain
