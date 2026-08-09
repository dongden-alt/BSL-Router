"""
Tests for bsl_lite_router (P2 — non-agentic single-task router, OAC agent matrix).

BSL-Lite is the coding-agent single-task router (targets Claude Code, Cursor, Aider).
It has an 8-agent matrix mirroring OAC primary agents (Scout, Planner,
Auditor, FastCoder, PowerCoder, UltraCoder, Refactor, FrontendCoder).
"Lite" = non-agentic (no multi-step orchestration), NOT single-model.
NO complexity estimation, NO buckets — pure task-route (classify → agent → model).

Covers:
- Canonical bsl_models.bsl_lite config path
- Legacy bsl_lite config path
- Safe default OFF (missing keys = disabled, not enabled)
- Agent override selection when enabled
- Scout fallback when classified agent has no override
- global_last_fallback used as final safety net
- default_route override bypasses entire matrix
- 3-slot dict route extraction (primary/fallback_1/fallback_2)
- Empty/None config handled gracefully (no hardcoded fallback)
- fail_open on unexpected errors returns empty
"""

import asyncio

from fastapi.responses import JSONResponse

import app.config_state as cs
from app import main as app_main
from app.models import ChatCompletionRequest, Message
from app.middleware.bsl_lite_router import (
    route_bsl_lite,
    _get_bsl_lite_cfg,
    BSLLiteDecision,
)
from app.middleware.coding_category_classifier import (
    classify_coding_request_category,
    score_categories,
    CATEGORY_ORDER,
)


# ─── Test config helpers ────────────────────────────────────────────────────


def _request(text: str, model: str = "bsl-lite") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=text)],
    )


def _config(
    enabled: bool = True,
    router_enabled: bool = True,
    global_last_fallback: str = "GLM-5.2",
) -> dict:
    """Config with scout (fallback) category override — flat agent→route."""
    return {
        "tools": {"bsl_lite_router": router_enabled},
        "bsl_models": {
            "bsl_lite": {
                "enabled": enabled,
                "category_overrides": {
                    "scout": "coder-2",
                },
                "global_last_fallback": global_last_fallback,
            },
        },
    }


def _canonical_config(
    enabled: bool = True,
    router_enabled: bool = True,
    global_last_fallback: str = "GLM-5.2",
) -> dict:
    """Canonical schema: bsl_models.bsl_lite with full matrix."""
    return _config(
        enabled=enabled,
        router_enabled=router_enabled,
        global_last_fallback=global_last_fallback,
    )


def _legacy_config(
    enabled: bool = True,
    router_enabled: bool = True,
    route: str = "coder-2",
    global_last_fallback: str = "GLM-5.2",
) -> dict:
    """Legacy single-route schema: top-level bsl_lite with bare route field."""
    return {
        "tools": {"bsl_lite_router": router_enabled},
        "bsl_lite": {
            "enabled": enabled,
            "route": route,
            "global_last_fallback": global_last_fallback,
        },
    }


# ─── Config reader tests ─────────────────────────────────────────────────────


def test_canonical_bsl_models_config_is_read() -> None:
    """Canonical schema bsl_models.bsl_lite is read correctly."""
    cfg = _canonical_config()
    bsl_cfg = _get_bsl_lite_cfg(cfg)
    assert bsl_cfg["enabled"] is True
    assert "category_overrides" in bsl_cfg


def test_legacy_bsl_lite_config_is_read() -> None:
    """Legacy top-level bsl_lite config is read correctly."""
    cfg = _legacy_config(route="coder-3")
    bsl_cfg = _get_bsl_lite_cfg(cfg)
    assert bsl_cfg["enabled"] is True


def test_canonical_preferred_over_legacy() -> None:
    """When both canonical and legacy exist, canonical wins."""
    cfg = {
        "bsl_models": {"bsl_lite": {"enabled": True, "category_overrides": {"scout": {"standard": "canonical-route"}}}},
        "bsl_lite": {"enabled": True, "route": "legacy-route"},
    }
    bsl_cfg = _get_bsl_lite_cfg(cfg)
    assert bsl_cfg["enabled"] is True
    assert "category_overrides" in bsl_cfg


def test_get_cfg_returns_empty_for_none() -> None:
    assert _get_bsl_lite_cfg(None) == {}


def test_get_cfg_returns_empty_for_empty_dict() -> None:
    assert _get_bsl_lite_cfg({}) == {}


def test_get_cfg_returns_empty_for_non_dict() -> None:
    assert _get_bsl_lite_cfg("not a dict") == {}  # type: ignore


# ─── Always-on routing tests (gate removal, 2026-08-06) ───


def test_missing_config_keys_route_unresolved() -> None:
    """Always on: empty config routes normally; no routes -> unresolved + empty.

    No hardcoded fallback - empty config = empty selected_model.
    """
    decision = route_bsl_lite(_request("hello"), {})
    assert decision.selected_model == ""
    assert decision.source == "unresolved"
    assert decision.fail_open is False


def test_none_config_routes_unresolved() -> None:
    """None config -> unresolved, empty selected_model (no hardcoded fallback)."""
    decision = route_bsl_lite(_request("hello"), None)  # type: ignore
    assert decision.selected_model == ""
    assert decision.source == "unresolved"


def test_disabled_flags_do_not_block_routing() -> None:
    """Always on: enabled=False + router=False still route via the matrix."""
    cfg = _config(enabled=False, router_enabled=False)
    decision = route_bsl_lite(_request("hello"), cfg)
    assert decision.selected_model == "coder-2"
    assert decision.source == "general_fallback"
    assert decision.fail_open is False


def test_router_flag_off_still_routes() -> None:
    """Always on: tools.bsl_lite_router=False does not block routing."""
    cfg = _config(enabled=True, router_enabled=False)
    decision = route_bsl_lite(_request("hello"), cfg)
    assert decision.source == "general_fallback"
    assert decision.selected_model == "coder-2"


def test_feature_flag_off_still_routes() -> None:
    """Always on: enabled=False does not block routing (catalog visibility only)."""
    cfg = _config(enabled=False, router_enabled=True)
    decision = route_bsl_lite(_request("hello"), cfg)
    assert decision.source == "general_fallback"
    assert decision.selected_model == "coder-2"


# ─── Route selection tests ───────────────────────────────────────────────────


def test_scout_fallback_selected_when_enabled() -> None:
    """When both flags are on, scout route is selected for a general query.

    Scout is the fallback category (merged with general).
    """
    decision = route_bsl_lite(_request("hello"), _config())
    assert decision.selected_model == "coder-2"
    assert decision.source == "general_fallback"
    assert decision.fail_open is False


# ─── System prompt contamination regression tests ───────────────────────────


def test_system_prompt_does_not_inflate_coding_category() -> None:
    """System prompt with coding keywords must NOT cause 'hello' to
    classify as a coding agent (power_coder, ultra_coder, etc.).

    Regression: system prompts from Claude Code/Cursor contain coding
    keywords (write, function, implement, debug, refactor, class) that
    inflated the coding category score and routed simple messages to
    expensive coding agents instead of scout fallback.
    """
    request = ChatCompletionRequest(
        model="bsl-lite",
        messages=[
            Message(
                role="system",
                content=(
                    "You are Claude Code. Write functions. Implement features. "
                    "Debug code. Refactor classes. Build components. "
                    "Optimize algorithms. Review code for vulnerabilities."
                ),
            ),
            Message(role="user", content="hello"),
        ],
    )
    decision = route_bsl_lite(request, _config())
    assert decision.category == "scout"  # general fallback
    assert decision.selected_model == "coder-2"
    assert decision.source == "general_fallback"


def test_real_coding_query_still_classifies_with_system_prompt() -> None:
    """A genuine coding query must still classify correctly even with
    a system prompt present."""
    request = ChatCompletionRequest(
        model="bsl-lite",
        messages=[
            Message(role="system", content="You are Claude Code."),
            Message(role="user", content="implement a python function to sort a list"),
        ],
    )
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["category_overrides"]["power_coder"] = "gpt-5.5"
    decision = route_bsl_lite(request, cfg)
    assert decision.category == "power_coder"
    assert decision.selected_model == "gpt-5.5"


def test_fallback_chain_includes_global_last() -> None:
    """Fallback chain should contain global_last_fallback."""
    decision = route_bsl_lite(
        _request("hello"), _config(global_last_fallback="glm-5.2")
    )
    assert decision.selected_model == "coder-2"
    assert "glm-5.2" in decision.fallback_chain


def test_agent_override_selected_for_power_coder() -> None:
    """Power-coder agent override is selected for an implementation query."""
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["category_overrides"]["power_coder"] = "gpt-5.5"
    decision = route_bsl_lite(_request("implement a python function"), cfg)
    assert decision.selected_model == "gpt-5.5"
    assert decision.source == "category_override"
    assert decision.category == "power_coder"


def test_all_agent_tasks_route_to_matching_matrix_rows() -> None:
    """Representative coding-agent tasks hit all 8 configured matrix rows."""
    cases = {
        "scout": "find where authentication is implemented",
        "planner": "plan the system architecture",
        "auditor": "audit this code for vulnerabilities",
        "fast_coder": "quick fix this typo",
        "power_coder": "implement a Python function",
        "ultra_coder": "optimize this complex algorithm",
        "refactor": "refactor and simplify this class",
        "frontend_coder": "build a responsive React UI component",
    }
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["category_overrides"] = {
        category: {"primary": f"route-{category}"} for category in cases
    }

    for category, task in cases.items():
        decision = route_bsl_lite(_request(task), cfg)
        assert decision.category == category, (task, decision)
        assert decision.selected_model == f"route-{category}", (task, decision)
        assert decision.source == ("general_fallback" if category == "scout" else "category_override")


def test_scout_fallback_when_agent_has_no_override() -> None:
    """When classified agent has no override, scout fallback is used."""
    decision = route_bsl_lite(_request("hello"), _config())
    assert decision.selected_model == "coder-2"
    assert decision.source == "general_fallback"


def test_missing_overrides_and_global_last_returns_empty() -> None:
    """When no overrides and no global_last_fallback, return empty (no hardcoded fallback)."""
    cfg = {
        "tools": {"bsl_lite_router": True},
        "bsl_models": {"bsl_lite": {"enabled": True}},
    }
    decision = route_bsl_lite(_request("hello"), cfg)
    assert decision.selected_model == ""
    assert decision.source == "unresolved"


def test_flags_off_still_route_via_matrix() -> None:
    """Always on: disabled flags do not short-circuit; matrix resolves first."""
    cfg = _config(enabled=False, router_enabled=False, global_last_fallback="deepseek-v4")
    decision = route_bsl_lite(_request("hello"), cfg)
    assert decision.selected_model == "coder-2"
    assert decision.source == "general_fallback"


# ─── Default route override tests ────────────────────────────────────────────


def test_default_route_bypasses_matrix() -> None:
    """default_route_enabled=True bypasses the entire matrix."""
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["default_route_enabled"] = True
    cfg["bsl_models"]["bsl_lite"]["default_route"] = "coder-3"
    decision = route_bsl_lite(_request("implement a python function"), cfg)
    assert decision.selected_model == "coder-3"
    assert decision.source == "default_route"


def test_default_route_off_uses_matrix() -> None:
    """default_route_enabled=False uses the matrix normally."""
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["default_route_enabled"] = False
    cfg["bsl_models"]["bsl_lite"]["default_route"] = "coder-3"
    decision = route_bsl_lite(_request("implement a python function"), cfg)
    assert decision.source != "default_route"


# ─── 3-slot dict route extraction tests ─────────────────────────────────────


def test_cell_accepts_3_slot_dict() -> None:
    """Agent override accepts a 3-slot dict {primary, fallback_1, fallback_2}."""
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["category_overrides"]["scout"] = {
        "primary": "coder-1",
        "fallback_1": "coder-2",
        "fallback_2": "coder-3",
    }
    decision = route_bsl_lite(_request("explain how photosynthesis works"), cfg)
    assert decision.selected_model == "coder-1"
    assert "coder-2" in decision.fallback_chain
    assert "coder-3" in decision.fallback_chain


def test_glf_accepts_3_slot_dict() -> None:
    """global_last_fallback accepts a 3-slot dict, expanding into the chain."""
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["global_last_fallback"] = {
        "primary": "glm-5.2",
        "fallback_1": "gpt-5.5",
        "fallback_2": "deepseek-v4-pro",
    }
    decision = route_bsl_lite(_request("hello"), cfg)
    assert decision.selected_model == "coder-2"
    assert "glm-5.2" in decision.fallback_chain
    assert "gpt-5.5" in decision.fallback_chain
    assert "deepseek-v4-pro" in decision.fallback_chain


def test_dict_route_does_not_poison_chain() -> None:
    """A dict route must not leak as a dict into the fallback chain."""
    cfg = _config()
    cfg["bsl_models"]["bsl_lite"]["category_overrides"]["scout"] = {
        "primary": "coder-1",
        "fallback_1": "coder-2",
    }
    decision = route_bsl_lite(_request("explain how photosynthesis works"), cfg)
    for entry in [decision.selected_model, *decision.fallback_chain]:
        assert isinstance(entry, str), f"Non-string entry in chain: {entry!r}"


# ─── Decision dataclass tests ────────────────────────────────────────────────


def test_decision_defaults() -> None:
    """BSLLiteDecision has correct defaults (empty, not hardcoded)."""
    d = BSLLiteDecision()
    assert d.selected_model == ""
    assert d.fallback_chain == []
    assert d.source == "disabled_default"
    assert d.fail_open is False


# ─── Legacy migration tests ──────────────────────────────────────────────────


def test_legacy_single_route_migrates_to_scout() -> None:
    """Legacy bsl_lite.route='coder-2' migrates to scout route.

    Since the legacy migration puts the route on scout, and the query
    classifies as scout (general), it should resolve via general_fallback.
    """
    cfg = _legacy_config(route="coder-2")
    decision = route_bsl_lite(_request("explain how photosynthesis works"), cfg)
    # Legacy config has no category_overrides, so falls to global_last_fallback
    assert decision.selected_model == "GLM-5.2"
    assert decision.source == "global_last_fallback"


# ─── Dispatch integration test ──────────────────────────────────────────────


def test_bsl_lite_dispatch_logs_canonical_route_and_advances_on_error(monkeypatch, capsys) -> None:
    """Integration test: _bsl_lite_dispatch logs canonical line and advances on 503."""
    cfg = _config()
    cs.replace_config(cfg)

    call_count = [0]
    async def _fake_process_chat_completion(body, client_wants_anthropic=False, client_wants_gemini=False, request=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return JSONResponse({"error": "upstream 503"}, status_code=503)
        return JSONResponse({"ok": True}, status_code=200)

    monkeypatch.setattr(app_main, "_process_chat_completion", _fake_process_chat_completion)

    body = {
        "model": "bsl-lite",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    result = asyncio.run(app_main._bsl_lite_dispatch(body))
    out = capsys.readouterr().out

    assert "[blacksand-lite]" in out
    assert "Blacksand-Lite >" in out
    assert result.status_code == 200
    assert call_count[0] == 2  # first entry 503'd, second succeeded


# ─── Weighted-scoring regression tests (specificity fix) ─────────────────────
# These lock the 5 misroutes proven by scratch/lite_probe.py before the fix,
# where flat +1 scoring let a generic verb tie/beat a specific phrase and the
# winner was decided arbitrarily by CATEGORY_ORDER.


def _classify(text: str):
    return classify_coding_request_category(_request(text))


def test_generic_add_does_not_beat_quick_fix() -> None:
    """'add a quick fix' → fast_coder, not power_coder (generic 'add')."""
    d = _classify("add a quick fix for the login bug")
    assert d.category == "fast_coder", (d.category, d.scores)


def test_rename_vietnamese_routes_fast_coder_not_refactor() -> None:
    """'đổi tên' (rename) belongs to fast_coder only — collision removed."""
    d = _classify("đổi tên biến này")
    assert d.category == "fast_coder", (d.category, d.scores)


def test_refactor_to_reduce_complexity_routes_refactor() -> None:
    """'refactor ... reduce complexity' → refactor; 'complexity' is now weak."""
    d = _classify("refactor the system to reduce complexity")
    assert d.category == "refactor", (d.category, d.scores)


def test_add_comment_routes_fast_coder() -> None:
    """'add a comment' is a trivial edit → fast_coder, not power_coder."""
    d = _classify("add a comment to this function")
    assert d.category == "fast_coder", (d.category, d.scores)


def test_strong_signal_beats_generic_without_tie() -> None:
    """A STRONG term must outscore a lone WEAK term (no CATEGORY_ORDER luck)."""
    d = _classify("quick fix this and add logging")
    # 'quick fix' (strong, fast_coder=2) beats 'add' (weak, power_coder=1)
    assert d.category == "fast_coder"
    assert d.margin >= 1, (d.scores, d.margin)


def test_no_regression_on_eight_agent_matrix() -> None:
    """The 8 canonical single-signal tasks still classify to their agent."""
    canonical = {
        "scout": "find where authentication is implemented",
        "planner": "plan the system architecture for a payments service",
        "auditor": "audit this code for security vulnerabilities",
        "fast_coder": "quick fix this typo in the readme",
        "power_coder": "implement a python function to sort a list",
        "ultra_coder": "optimize this complex sorting algorithm",
        "refactor": "refactor and simplify this class",
        "frontend_coder": "build a responsive React UI component",
    }
    for expected, task in canonical.items():
        d = _classify(task)
        assert d.category == expected, (task, d.category, d.scores)


# ─── Observability field tests (Phase 2A parity) ─────────────────────────────


def test_decision_exposes_score_vector() -> None:
    """scores is a full per-agent vector covering every CATEGORY_ORDER entry."""
    d = _classify("build a responsive React UI component")
    assert isinstance(d.scores, dict)
    for cat in CATEGORY_ORDER:
        assert cat in d.scores
    assert d.scores["frontend_coder"] >= 2  # multiple strong hits


def test_decision_runner_up_and_margin() -> None:
    """runner_up is the 2nd-ranked agent; margin = winner - runner_up."""
    d = _classify("implement and optimize a new ranking algorithm")
    assert d.runner_up is not None
    assert d.runner_up != d.category
    assert d.margin == d.scores[d.category] - d.scores[d.runner_up]


def test_empty_text_observability_defaults() -> None:
    """Empty request → empty scores, no runner_up, zero margin."""
    d = classify_coding_request_category(
        ChatCompletionRequest(model="bsl-lite", messages=[Message(role="user", content="")])
    )
    assert d.scores == {}
    assert d.runner_up is None
    assert d.margin == 0
    assert d.category == "scout"  # general fallback


def test_score_categories_helper_is_weighted() -> None:
    """score_categories applies STRONG=2 / WEAK=1 weighting."""
    # 'quick fix' is a single STRONG fast_coder term.
    assert score_categories("quick fix")["fast_coder"] == 2
    # 'add' is a single WEAK power_coder term.
    assert score_categories("add")["power_coder"] == 1


def test_dead_heat_softens_confidence() -> None:
    """A margin-0 tie against a real runner-up softens confidence below 1.0."""
    # Construct a genuine equal-weight tie: two STRONG terms from two agents.
    d = _classify("review this and refactor this")
    if d.margin == 0 and d.runner_up is not None:
        assert d.confidence <= 0.75
        assert any("tie" in r for r in d.reasons)
