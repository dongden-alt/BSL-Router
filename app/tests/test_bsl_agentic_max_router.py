"""
Tests for bsl_agentic_max_router (multi-domain fusion, depth=balanced LOCKED).

Covers:
- Canonical + legacy config read
- ALWAYS ON: empty config resolves to unresolved, flag-off still routes
- Coding domain wins on strong coding signal
- Chat domain wins on strong chat signal
- confidence_weighted strategy
- coding_priority / chat_priority strategies
- Cross-domain fallback appended to chain
- Depth always "balanced"
"""

from app.models import ChatCompletionRequest, Message
from app.middleware.bsl_agentic_max_router import (
    route_bsl_agentic_max,
    _get_bsl_agentic_max_cfg,
    _pick_domain,
    AGENTIC_MAX_DEPTH,
)


def _request(text: str, model: str = "blacksand-agentic-max") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=text)],
    )


def _config(strategy: str = "confidence_weighted", router_enabled: bool = True) -> dict:
    return {
        "tools": {"bsl_agentic_max_router": router_enabled},
        "bsl_models": {
            "bsl_agentic_max": {
                "enabled": True,
                "merge_strategy": strategy,
                "agent_routes": {
                    "scout": {"primary": "code-scout"},
                    "power_coder": {"primary": "code-power", "fallback_1": "cp1"},
                },
                "chat_routes": {
                    "general": {
                        "fast": {"primary": "chat-fast"},
                        "standard": {"primary": "chat-std"},
                        "deep": {"primary": "chat-deep"},
                    },
                    "technical": {
                        "standard": {"primary": "chat-tech"},
                    },
                },
                "global_last_fallback": "GLM-5.2",
            },
        },
    }


# ─── Config reader ───────────────────────────────────────────────────────────


def test_canonical_config_read() -> None:
    assert _get_bsl_agentic_max_cfg(_config())["enabled"] is True


def test_legacy_config_read() -> None:
    cfg = {"bsl_agentic_max": {"enabled": True}}
    assert _get_bsl_agentic_max_cfg(cfg)["enabled"] is True


def test_get_cfg_none() -> None:
    assert _get_bsl_agentic_max_cfg(None) == {}


# ─── Domain picker ──────────────────────────────────────────────────────────


def test_pick_domain_confidence_weighted() -> None:
    assert _pick_domain("confidence_weighted", 1.0, 0.5) == "coding"
    assert _pick_domain("confidence_weighted", 0.5, 1.0) == "chat"
    assert _pick_domain("confidence_weighted", 0.5, 0.5) == "chat"  # tie -> chat


def test_pick_domain_coding_priority() -> None:
    assert _pick_domain("coding_priority", 1.0, 1.0) == "coding"
    assert _pick_domain("coding_priority", 0.0, 1.0) == "chat"  # no coding signal


def test_pick_domain_chat_priority() -> None:
    assert _pick_domain("chat_priority", 1.0, 1.0) == "chat"
    assert _pick_domain("chat_priority", 1.0, 0.0) == "coding"  # no chat signal


# ─── ALWAYS ON (2026-08-06 directive) ─────────────────────────────────────


def test_empty_config_unresolved() -> None:
    """Empty config: no flag gate anymore, but with no matrix at all the
    router resolves to an empty selected model and source='unresolved'.
    """
    d = route_bsl_agentic_max(_request("hello"), {})
    assert d.selected_model == ""
    assert d.source == "unresolved"


def test_router_flag_ignored_always_on() -> None:
    """ALWAYS ON: tools.bsl_agentic_max_router=False does not block routing.
    With a populated matrix the fusion still runs and picks a coding route.
    """
    d = route_bsl_agentic_max(_request("implement"), _config(router_enabled=False))
    assert d.domain == "coding"
    assert d.selected_model == "code-power"


# ─── Fusion routing ─────────────────────────────────────────────────────────


def test_coding_domain_wins_on_coding_signal() -> None:
    d = route_bsl_agentic_max(_request("implement a new parser function"), _config())
    assert d.domain == "coding"
    assert d.selected_model == "code-power"
    # chat route appended as cross-domain fallback
    assert any(m.startswith("chat-") for m in d.fallback_chain)


def test_chat_domain_wins_on_chat_signal() -> None:
    d = route_bsl_agentic_max(_request("hello, how are you today?"), _config())
    assert d.domain == "chat"
    assert d.selected_model in ("chat-fast", "chat-std", "chat-deep", "GLM-5.2")


def test_coding_priority_strategy() -> None:
    d = route_bsl_agentic_max(_request("implement a function"), _config(strategy="coding_priority"))
    assert d.domain == "coding"


def test_depth_always_balanced() -> None:
    d = route_bsl_agentic_max(_request("implement a function"), _config())
    assert d.depth == "balanced"
    assert AGENTIC_MAX_DEPTH == "balanced"


def test_global_last_fallback() -> None:
    cfg = {
        "tools": {"bsl_agentic_max_router": True},
        "bsl_models": {"bsl_agentic_max": {"enabled": True, "agent_routes": {}, "chat_routes": {}, "global_last_fallback": "GLM-5.2"}},
    }
    d = route_bsl_agentic_max(_request("do something"), cfg)
    assert d.selected_model == "GLM-5.2"
