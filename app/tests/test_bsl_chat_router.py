"""
Tests for bsl-chat router and category classifier (v2.1 pipeline).

Covers:
- 13-category taxonomy (incl. research/science/lifestyle/philosophy/finance)
- finance vs business keyword collision fix
- canonical bsl_models.bsl_chat config path
- legacy bsl_chat config path
- always-on routing (missing keys = unresolved/empty, not disabled)
- global_last_fallback used when no category override matches (always active)
- stale global_last_fallback_enabled=false does NOT disable the fallback
- general category fallback when classified category has no override
- coarse deterministic confidence (0.0/0.5/1.0)
- complexity bucket routing
- category overrides
- no hardcoded FALLBACK_MODEL — empty selected_model when nothing configured
- default_route ON/OFF with global_last_fallback as final safety net
"""

import asyncio
import re

from fastapi.responses import JSONResponse

import app.config_state as cs
from app import main as app_main
from app.models import ChatCompletionRequest, Message
from app.middleware.category_classifier import classify_request_category
from app.middleware.bsl_chat_router import route_bsl_chat
from app.middleware.task_complexity import _count_distinct_matches


def _request(text: str, model: str = "bsl-chat") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=text)],
    )


def _config(
    enabled: bool = True,
    router_enabled: bool = True,
    global_last_fallback_enabled: bool = True,
) -> dict:
    """Config with general category overrides (new pipeline v2.1).

    No default_combo, no default_combo_by_complexity.
    general category covers fast/standard/deep as the fallback category.
    """
    return {
        "tools": {"bsl_chat_router": router_enabled},
        "bsl_chat": {
            "enabled": enabled,
            "category_overrides": {
                "general": {
                    "fast": "coder-1",
                    "standard": "coder-2",
                    "deep": "coder-3",
                },
            },
            "global_last_fallback": "GLM-5.2",
            "global_last_fallback_enabled": global_last_fallback_enabled,
        },
    }


def _canonical_config(
    enabled: bool = True,
    router_enabled: bool = True,
    global_last_fallback_enabled: bool = True,
) -> dict:
    """Canonical future schema: bsl_models.bsl_chat instead of top-level bsl_chat."""
    return {
        "tools": {"bsl_chat_router": router_enabled},
        "bsl_models": {
            "bsl_chat": {
                "enabled": enabled,
                "category_overrides": {
                    "general": {
                        "fast": "coder-1",
                        "standard": "coder-2",
                        "deep": "coder-3",
                    },
                },
                "global_last_fallback": "GLM-5.2",
                "global_last_fallback_enabled": global_last_fallback_enabled,
            },
        },
    }


def _repeat_words(words: list[str], count: int) -> str:
    return " ".join(words * count)


# ─── Category classification tests ─────────────────────────────────────────


def test_classifies_technical_english_prompt() -> None:
    decision = classify_request_category(
        _request("Debug this FastAPI middleware traceback and fix the Python router bug.")
    )
    assert decision.category == "technical"
    assert decision.score >= 2


# ─── System prompt contamination regression tests ───────────────────────────


def test_system_prompt_does_not_inflate_category() -> None:
    """System prompt with technical keywords must NOT cause 'hello' to
    classify as technical.

    Regression: system prompts from Antigravity/Claude Code contain coding
    keywords (code, debug, router, middleware, python, function) that
    inflated the category score to technical even for simple greetings.
    """
    request = ChatCompletionRequest(
        model="bsl-chat",
        messages=[
            Message(
                role="system",
                content=(
                    "You are a coding assistant. Debug Python code. "
                    "Write functions. Fix bugs in FastAPI middleware. "
                    "Refactor classes. Deploy router applications."
                ),
            ),
            Message(role="user", content="hello"),
        ],
    )
    decision = classify_request_category(request)
    assert decision.category == "general"
    assert decision.score == 0


def test_system_prompt_does_not_inflate_complexity() -> None:
    """System prompt with technical keywords must NOT cause 'hello' to
    route to deep complexity bucket."""
    request = ChatCompletionRequest(
        model="bsl-chat",
        messages=[
            Message(
                role="system",
                content=(
                    "You are a coding assistant. Debug Python code. "
                    "Write functions. Fix bugs in FastAPI middleware. "
                    "Refactor classes. Deploy router applications."
                ),
            ),
            Message(role="user", content="hello"),
        ],
    )
    decision = route_bsl_chat(request, _config())
    assert decision.complexity_bucket == "fast"
    assert decision.category == "general"


def test_system_prompt_does_not_inflate_what_model_query() -> None:
    """'What model am I talking to' must route to general+fast even with
    a heavy coding system prompt."""
    request = ChatCompletionRequest(
        model="bsl-chat",
        messages=[
            Message(
                role="system",
                content=(
                    "You are Claude Code, Anthropic's official CLI. "
                    "You debug code, write functions, implement features, "
                    "refactor architecture, and deploy applications."
                ),
            ),
            Message(role="user", content="what model am I talking to?"),
        ],
    )
    decision = route_bsl_chat(request, _config())
    assert decision.complexity_bucket == "standard", \
        "funnel: 'what model am I' is not a trivial greeting, defaults to standard"
    assert decision.category == "general"


def test_real_user_query_still_classifies_with_system_prompt() -> None:
    """A genuine technical query must still classify as technical even
    with a system prompt present."""
    request = ChatCompletionRequest(
        model="bsl-chat",
        messages=[
            Message(role="system", content="You are a helpful assistant."),
            Message(
                role="user",
                content="Debug this Python FastAPI router bug and fix the middleware error.",
            ),
        ],
    )
    decision = classify_request_category(request)
    assert decision.category == "technical"
    assert decision.score >= 2


def test_classifies_vietnamese_legal_prompt() -> None:
    decision = classify_request_category(
        _request("Hãy phân tích hợp đồng này theo luật Việt Nam và các điều khoản pháp lý.")
    )
    assert decision.category == "law"
    assert decision.score >= 2


def test_tie_breaking_is_deterministic_by_category_order() -> None:
    decision = classify_request_category(_request("Python contract"))
    assert decision.reasons == ["technical=1", "law=1"]
    assert decision.category == "general"
    assert decision.score == 1


def test_vietnamese_viet_no_longer_pushes_creative_signal_over_threshold() -> None:
    decision = classify_request_category(_request("Hãy viết truyện ngắn."))
    assert decision.category == "general"
    assert decision.score == 1
    assert decision.reasons == ["creative=1"]


def test_finance_beats_business_on_revenue_prompt() -> None:
    """revenue/profit are finance-primary; business no longer competes."""
    decision = classify_request_category(
        _request("Analyze the revenue and profit margins for this investment fund.")
    )
    assert decision.category == "finance"
    assert decision.score >= 2


def test_finance_beats_business_on_vietnamese_doanh_thu() -> None:
    """doanh thu is finance-primary in Vietnamese."""
    decision = classify_request_category(
        _request("Đánh giá doanh thu và lợi nhuận của quỹ đầu tư này.")
    )
    assert decision.category == "finance"
    assert decision.score >= 2


def test_classifies_research_category() -> None:
    decision = classify_request_category(
        _request("Conduct a literature review and survey for this research study.")
    )
    assert decision.category == "research"
    assert decision.score >= 2


def test_classifies_science_category() -> None:
    decision = classify_request_category(
        _request("The physics experiment tests quantum theory in chemistry biology.")
    )
    assert decision.category == "science"
    assert decision.score >= 2


def test_classifies_lifestyle_category() -> None:
    decision = classify_request_category(
        _request("Share a travel food recipe for cooking and fitness wellness.")
    )
    assert decision.category == "lifestyle"
    assert decision.score >= 2


def test_classifies_philosophy_category() -> None:
    decision = classify_request_category(
        _request("Discuss the philosophy of ethics, morality, and consciousness.")
    )
    assert decision.category == "philosophy"
    assert decision.score >= 2


def test_confidence_is_coarse_deterministic() -> None:
    """No more fake-precision score/10.0 float; use 0.0/0.5/1.0."""
    # score 0 → 0.0
    assert classify_request_category(_request("hello world")).confidence == 0.0
    # score 1 → 0.5
    assert classify_request_category(_request("Python contract")).confidence == 0.5
    # score 2+ → 1.0
    assert classify_request_category(
        _request("Debug this Python router bug.")
    ).confidence == 1.0


# ─── Router routing tests (v2.1 pipeline) ──────────────────────────────────


def test_short_prompt_routes_to_general_standard_bucket() -> None:
    """Short prompt with analytical verb → general → standard bucket → coder-2."""
    decision = route_bsl_chat(
        _request("Explain the design tradeoffs for this middleware architecture."),
        _config(),
    )
    assert decision.selected_model == "coder-2"
    assert decision.complexity_bucket == "standard"
    # funnel: analytical verb ('Explain' / 'tradeoffs') + domain density ->
    # defaults to standard (no trivial pattern match, no deep signals)
    assert decision.source == "general_fallback"


def test_standard_prompt_routes_to_general_standard_bucket() -> None:
    standard_prompt = "Compare architectural tradeoffs " + _repeat_words(["analysis"], 600)
    decision = route_bsl_chat(_request(standard_prompt), _config())
    assert decision.selected_model == "coder-2"
    assert decision.complexity_bucket == "standard"
    assert decision.source == "general_fallback"


def test_deep_prompt_routes_to_general_deep_bucket() -> None:
    deep_prompt = "Implement debug refactor test integrate migrate " + _repeat_words(["analysis"], 240)
    decision = route_bsl_chat(_request(deep_prompt), _config())
    assert decision.selected_model == "coder-3"
    assert decision.complexity_bucket == "deep"
    assert decision.source == "general_fallback"


def test_trivial_prompt_routes_to_general_fast_bucket() -> None:
    decision = route_bsl_chat(_request("hi"), _config())
    assert decision.selected_model == "coder-1"
    assert decision.complexity_bucket == "fast"
    assert decision.source == "general_fallback"


def test_category_override_wins_over_general_fallback() -> None:
    """When a specific category has an override, it wins over general."""
    cfg = _config()
    cfg["bsl_chat"]["category_overrides"]["technical"] = {"standard": "coder-3"}
    decision = route_bsl_chat(
        _request("Refactor this Python FastAPI router middleware and explain the API design."),
        cfg,
    )
    assert decision.category == "technical"
    assert decision.complexity_bucket == "standard", \
        "funnel: 'Refactor' alone doesn't reach deep; analytical 'explain' + domain density -> standard"
    assert decision.selected_model == "coder-3"
    assert decision.source == "category_override"


def test_general_fallback_used_when_category_has_no_override() -> None:
    """When the classified category has no override, fall back to general."""
    cfg = _config()
    # Only general has overrides; technical has none.
    decision = route_bsl_chat(
        _request("Debug this Python router bug."),  # technical category
        cfg,
    )
    # Falls back to general[standard] = coder-2 (funnel: 'Debug' triggers workflow
    # verb but D4 not saturated and no D3 density -> standard)
    assert decision.selected_model == "coder-2"
    assert decision.complexity_bucket == "standard", \
        "funnel: single 'Debug' verb doesn't saturate D4; defaults to standard"
    assert decision.source == "general_fallback"


# ─── Canonical config + disabled tests ─────────────────────────────────────


def test_canonical_bsl_models_config_works() -> None:
    """Canonical future schema bsl_models.bsl_chat is read correctly."""
    deep_prompt = "Implement debug refactor test integrate migrate " + " ".join(["analysis"] * 240)
    decision = route_bsl_chat(
        _request(deep_prompt),
        _canonical_config(),
    )
    assert decision.selected_model == "coder-3"
    assert decision.complexity_bucket == "deep"
    assert decision.source == "general_fallback"


def test_missing_config_keys_route_through_matrix() -> None:
    """Always-on: empty config routes through matrix; no overrides = unresolved."""
    decision = route_bsl_chat(
        _request("Debug this Python router bug."),
        {},  # completely empty config
    )
    # No config → no overrides, no global_last_fallback → empty selected_model.
    assert decision.selected_model == ""
    assert decision.fail_open is False
    assert decision.source == "unresolved"


def test_stale_disabled_flags_still_route_through_matrix() -> None:
    """Always-on: stale enabled=False flags no longer gate routing."""
    decision = route_bsl_chat(
        _request("Debug this Python router bug."),
        _config(enabled=False, router_enabled=False),
    )
    # Routing is always-on; technical category, standard bucket → general fallback coder-2
    assert decision.selected_model == "coder-2"
    assert decision.source == "general_fallback"
    assert decision.fail_open is False


def test_global_last_fallback_used_when_no_overrides() -> None:
    """When no category overrides at all, use global_last_fallback."""
    cfg = _config()
    cfg["bsl_chat"]["category_overrides"] = {}  # remove all overrides
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.selected_model == "GLM-5.2"
    assert decision.source == "global_last_fallback"


def test_stale_global_last_fallback_enabled_false_does_not_disable() -> None:
    """Global Last Fallback has no toggle. A stale
    ``global_last_fallback_enabled: false`` must NOT prevent it from being used."""
    cfg = _config(global_last_fallback_enabled=False)
    cfg["bsl_chat"]["category_overrides"] = {}  # remove all overrides
    decision = route_bsl_chat(_request("hi"), cfg)
    # Still uses global_last_fallback despite the stale false toggle
    assert decision.selected_model == "GLM-5.2"
    assert decision.source == "global_last_fallback"


def test_none_config_routes_through_matrix() -> None:
    """None config is handled gracefully; no hardcoded fallback."""
    decision = route_bsl_chat(_request("Debug this Python router bug."), None)
    # No config → no overrides, no global_last_fallback → empty.
    assert decision.selected_model == ""
    assert decision.category == "technical"
    assert decision.complexity_bucket == "standard", \
        "funnel: single 'Debug' verb without D3 density -> standard"
    assert decision.source == "unresolved"
    assert decision.fail_open is False


# ─── v2.1 pipeline toggle tests ────────────────────────────────────────────
# Global Last Fallback is always active when configured — there is no disable
# toggle. These tests confirm the toggle no longer gates the fallback step.


def test_global_last_fallback_no_toggle_still_uses_category_overrides() -> None:
    """Category overrides still win; the removed toggle does not affect them."""
    cfg = _config(global_last_fallback_enabled=False)
    decision = route_bsl_chat(_request("hi"), cfg)
    # general[fast] override still works
    assert decision.selected_model == "coder-1"
    assert decision.source == "general_fallback"


def test_global_last_fallback_no_toggle_no_overrides_uses_fallback() -> None:
    """No overrides + stale false toggle = global_last_fallback still used."""
    cfg = _config(global_last_fallback_enabled=False)
    cfg["bsl_chat"]["category_overrides"] = {}
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.selected_model == "GLM-5.2"
    assert decision.source == "global_last_fallback"


# ─── v2 Option E calibration tests ─────────────────────────────────────────


def test_repeated_analysis_does_not_inflate_to_deep() -> None:
    """analysis×600 with no other depth signals must NOT route to deep."""
    prompt = "Summary " + _repeat_words(["analysis"], 600)
    decision = route_bsl_chat(_request(prompt), _config())
    assert decision.complexity_bucket in ("fast", "standard")
    assert decision.complexity_bucket != "deep"


def test_long_paste_with_summarize_stays_standard() -> None:
    """Long pasted context + 'summarize' should not auto-route to deep."""
    prompt = "summarize " + " ".join(["context"] * 800)
    decision = route_bsl_chat(_request(prompt), _config())
    assert decision.complexity_bucket != "deep"


def test_philosophy_prompt_routes_to_standard() -> None:
    """Philosophy analysis without output scope → standard (funnel: D3 alone is not enough)."""
    prompt = (
        "Analyze and compare the ethics of utilitarianism vs deontology "
        "from multiple perspectives. Examine the pros and cons, cite sources "
        "from the literature, and reason from first principles about the "
        "underlying framework."
    )
    decision = route_bsl_chat(_request(prompt), _config())
    # funnel: D3=True but D1=False, D2=False, D4=False -> (False v False v False) AND True = False
    # Cognitive depth without output scope requirement is standard, not deep.
    assert decision.complexity_bucket == "standard", \
        "funnel: D3 alone fires but no D1 scope or D4 workflow for two-gate deep"


def test_research_prompt_routes_to_deep() -> None:
    """Research/evidence markers should route to deep."""
    prompt = (
        "Conduct a comprehensive literature review. Cite sources, gather "
        "evidence and statistics, and synthesize the findings with "
        "rigorous methodology."
    )
    decision = route_bsl_chat(_request(prompt), _config())
    assert decision.complexity_bucket == "deep"


def test_vietnamese_diacritic_deep_prompt_routes_to_standard() -> None:
    """Vietnamese diacritic deep → standard (funnel: D3 alone without D1/D4)."""
    prompt = (
        "Hãy phân tích và so sánh triết học đạo đức từ nhiều góc nhìn. "
        "Xem xét ưu và nhược điểm, trích dẫn nguồn, và luận từ gốc rễ "
        "của vấn đề."
    )
    decision = route_bsl_chat(_request(prompt), _config())
    # funnel: D3=True but D1=False (no 'báo cáo' or word count), D4=False -> standard
    assert decision.complexity_bucket == "standard", \
        "funnel: VI D3 fires but no D1 scope or D4 workflow for deep"


def test_vietnamese_nondiacritic_deep_prompt_routes_to_standard() -> None:
    """Vietnamese non-diacritic deep → standard (funnel: D3 alone without D1/D4)."""
    prompt = (
        "Hay phan tich va so sanh triet hoc dao duc tu nhieu goc nhin. "
        "Xet xem uu va nhuoc diem, trich dan nguon, va luan tu goc re "
        "cua van de."
    )
    decision = route_bsl_chat(_request(prompt), _config())
    # funnel: D3=True but D1=False (no 'bao cao' or word count), D4=False -> standard
    assert decision.complexity_bucket == "standard", \
        "funnel: VI D3 fires but no D1 scope or D4 workflow for deep"


def test_count_distinct_matches_tolerates_capture_groups() -> None:
    """Regression: multi-group patterns must not crash the counter.

    findall() on a 2+ capture-group pattern returns list[tuple]; the old
    implementation crashed on .lower(). The finditer()-based helper must
    normalize on group(1) and count distinct stems without raising.
    """
    # Two capturing groups -> findall would yield tuples like ('analyze', 'ze').
    nested = re.compile(r"\b(analy(ze|se))\b", re.IGNORECASE)
    text = "analyze ANALYZE analyse"
    # Must not raise, and must count distinct group(1) stems: {analyze, analyse}.
    assert _count_distinct_matches(nested, text) == 2

    # Zero capture groups -> normalizes on the full match.
    zero_group = re.compile(r"\bfoo\b", re.IGNORECASE)
    assert _count_distinct_matches(zero_group, "foo FOO foo") == 1


# ─── Default Route override tests ───────────────────────────────────────────


def test_default_route_bypasses_category_overrides() -> None:
    """When default_route_enabled=True, all requests route to default_route,
    bypassing the entire category×complexity matrix."""
    cfg = _config()
    cfg["bsl_chat"]["default_route_enabled"] = True
    cfg["bsl_chat"]["default_route"] = "coder-3"
    # This prompt would normally route to general[fast]=coder-1
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.selected_model == "coder-3"
    assert decision.source == "default_route"
    assert "bypassed" in decision.reasons[1]


def test_default_route_disabled_falls_through_to_category() -> None:
    """When default_route_enabled=False (or missing), normal category routing applies."""
    cfg = _config()
    cfg["bsl_chat"]["default_route_enabled"] = False
    cfg["bsl_chat"]["default_route"] = "coder-3"
    # Short prompt → general[fast]=coder-1 (not coder-3)
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.selected_model == "coder-1"
    assert decision.source == "general_fallback"


def test_default_route_enabled_but_no_model_falls_through() -> None:
    """When default_route_enabled=True but default_route is empty/missing,
    the router falls through to normal category routing."""
    cfg = _config()
    cfg["bsl_chat"]["default_route_enabled"] = True
    # default_route not set → should fall through to general[fast]=coder-1
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.selected_model == "coder-1"
    assert decision.source == "general_fallback"


def test_default_route_with_v2_dict_cell() -> None:
    """default_route supports the v2 3-slot dict schema (primary + fallbacks)."""
    cfg = _config()
    cfg["bsl_chat"]["default_route_enabled"] = True
    cfg["bsl_chat"]["default_route"] = {
        "primary": "coder-3",
        "fallback_1": "coder-2",
        "fallback_2": "coder-1",
    }
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.selected_model == "coder-3"
    assert decision.fallback_chain == ["coder-2", "coder-1"]
    assert decision.source == "default_route"


# ─── General fallback appended after category override ──────────────────────


def test_category_override_appends_general_fallback() -> None:
    """When a category override is selected, General's P/F1/F2 are appended
    to the fallback_chain (deduped by the dispatcher)."""
    cfg = _config()
    cfg["bsl_chat"]["category_overrides"] = {
        "technical": {
            "standard": {
                "primary": "coder-3",
                "fallback_1": "coder-2",
                "fallback_2": "coder-1",
            },
        },
        "general": {
            "standard": {
                "primary": "gpt-5.5",
                "fallback_1": "claude-opus",
                "fallback_2": "glm-5.2",
            },
        },
    }
    decision = route_bsl_chat(_request("write a python function to sort a list"), cfg)
    # funnel: simple code request -> technical + standard (not fast)
    assert decision.source == "category_override"
    assert decision.selected_model == "coder-3"
    assert "gpt-5.5" in decision.fallback_chain
    assert "claude-opus" in decision.fallback_chain
    assert "glm-5.2" in decision.fallback_chain


def test_general_selection_does_not_duplicate() -> None:
    """When source is general_fallback (no category override matched), the
    general cell is NOT duplicated in the fallback_chain."""
    cfg = _config()
    # Only general configured; no other category overrides.
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.source == "general_fallback"
    assert decision.selected_model == "coder-1"
    # fallback_chain should be empty (only primary configured in _config)
    assert decision.fallback_chain == []


def test_default_route_does_not_append_general() -> None:
    """When default_route is ON, no category override or general fallback is
    consulted — the chain is purely from default_route."""
    cfg = _config()
    cfg["bsl_chat"]["default_route_enabled"] = True
    cfg["bsl_chat"]["default_route"] = "coder-3"
    cfg["bsl_chat"]["category_overrides"] = {
        "technical": {
            "fast": {"primary": "tech-1"},
        },
        "general": {
            "fast": {"primary": "gen-1"},
        },
    }
    decision = route_bsl_chat(_request("hi"), cfg)
    assert decision.source == "default_route"
    assert decision.selected_model == "coder-3"
    assert decision.fallback_chain == []


def test_category_override_without_general_config() -> None:
    """Category override works without error when no general fallback is
    configured at all."""
    cfg = _config()
    cfg["bsl_chat"]["category_overrides"] = {
        "technical": {
            "standard": {"primary": "tech-1"},
        },
        # No "general" key at all.
    }
    decision = route_bsl_chat(_request("write a python function"), cfg)
    # funnel: simple code request -> technical + standard (not fast)
    assert decision.source == "category_override"
    assert decision.selected_model == "tech-1"
    assert decision.fallback_chain == []


def test_category_override_with_general_but_different_bucket() -> None:
    """Category override appends general only for the matching bucket."""
    cfg = _config()
    cfg["bsl_chat"]["category_overrides"] = {
        "technical": {
            "standard": {"primary": "tech-1"},
        },
        "general": {
            "fast": {"primary": "gen-fast"},  # different bucket
        },
    }
    decision = route_bsl_chat(_request("write a python function"), cfg)
    # funnel: simple code request -> technical + standard (not fast)
    assert decision.source == "category_override"
    assert decision.selected_model == "tech-1"
    # general[standard] not configured -> nothing appended
    assert decision.fallback_chain == []


def test_bsl_matrix_dispatch_logs_canonical_combo_model_and_complexity(monkeypatch, capsys) -> None:
    cfg = _canonical_config()
    cfg["providers"] = {
        "vsllm-gpt": {
            "models": [
                {"id": "gpt-5.6-sol", "enabled": True},
                {"id": "gpt-5.6-sol-pro20x", "enabled": True},
            ],
        },
    }
    cfg["bsl_models"]["bsl_chat"]["category_overrides"]["general"]["fast"] = "GPT-5.6-SOL"

    cs.replace_config(cfg)

    async def _fake_process_chat_completion(body, client_wants_anthropic=False, client_wants_gemini=False, request=None):
        return JSONResponse({"ok": True}, status_code=200)

    monkeypatch.setattr(app_main, "_process_chat_completion", _fake_process_chat_completion)

    body = {
        "model": "bsl-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    result = asyncio.run(app_main._bsl_matrix_dispatch(body))
    out = capsys.readouterr().out

    assert "[blacksand-chat Matrix]" in out
    assert "Blacksand-Chat > GPT-5.6-SOL > vsllm-gpt/gpt-5.6-sol + general + fast" in out
    assert result.status_code == 200


# ─── Phase 2: surfaced category score vector ───────────────────────────────


def test_category_scores_vector_is_surfaced() -> None:
    """Phase 2: the full per-category score dict is now returned, not discarded."""
    decision = classify_request_category(_request("Debug this Python router bug."))
    assert isinstance(decision.scores, dict)
    # every scored category from CATEGORY_ORDER must be present as a key
    from app.middleware.category_classifier import CATEGORY_ORDER
    for cat in CATEGORY_ORDER:
        assert cat in decision.scores
    assert decision.scores["technical"] >= 2


def test_multi_domain_flag_true_for_two_strong_categories() -> None:
    """A prompt hitting two categories above threshold sets multi_domain=True."""
    decision = classify_request_category(
        _request("Analyze the legal contract terms and the financial revenue and profit implications for this investment fund.")
    )
    assert decision.multi_domain is True
    # runner_up must be a real, different category from the winner
    assert decision.runner_up is not None
    assert decision.runner_up != decision.category


def test_single_domain_is_not_multi_domain() -> None:
    """A clean single-domain prompt must NOT be flagged multi_domain."""
    decision = classify_request_category(_request("Debug this Python router bug and fix the middleware."))
    assert decision.multi_domain is False


def test_empty_text_has_safe_vector_defaults() -> None:
    """Empty-text path returns safe defaults for the new fields."""
    from app.models import ChatCompletionRequest, Message
    req = ChatCompletionRequest(model="bsl-chat", messages=[Message(role="user", content="")])
    decision = classify_request_category(req)
    assert decision.scores == {}
    assert decision.runner_up is None
    assert decision.multi_domain is False


# ─── Phase 2C: D2 wiring integration tests ────────────────────────────────
# These prove D2 now fires through the router when category_scores are wired
# into estimate_request_complexity.


def test_multi_domain_dense_routes_to_deep() -> None:
    """Multi-domain (law+finance) + dense (>=120 chars, >=3 connectors) →
    DEEP bucket via D2 AND D3 two-gate."""
    prompt = (
        "Analyze the legal contract and the financial revenue and the tax "
        "implications and the compliance requirements for this merger deal."
    )
    decision = route_bsl_chat(_request(prompt), _config())
    # D2: law (legal, contract) + finance (financial, revenue) each >=2
    # D3: 4 'and' connectors, len=131 >=120
    # D8: head verb 'Analyze' is not low-reasoning
    # deep = (D1 v D2 v D4) AND D3 = (F v T v F) AND T = T
    assert decision.complexity_bucket == "deep", \
        "D2 (multidomain) + D3 (density) must route multi-domain+dense to deep"
    assert decision.category in ("law", "finance"), \
        "multi-domain prompt should classify as law or finance"
    # Verify D2 flag is visible in decision
    assert any("multi_domain" in r for r in decision.reasons), \
        "D2 multi_domain reason should be appended to reasons"


def test_single_domain_long_stays_standard() -> None:
    """Single-domain request with length/density but no D2 → standard."""
    prompt = (
        "Tell me a detailed story about a brave wizard who explores "
        "an ancient castle and discovers a hidden treasure and meets "
        "a mysterious dragon and learns about their magical bond and "
        "saves the kingdom from destruction."
    )
    decision = route_bsl_chat(_request(prompt), _config())
    # Only creative (story) score=1, below CATEGORY_SCORE_THRESHOLD of 2 → general
    # D2=false, D3=true, D4=false, D1=false → (F v F v F) AND T = F → standard
    assert decision.complexity_bucket == "standard", \
        "single-domain request must NOT be promoted to deep by D3 alone"
    assert decision.category == "general"
    assert not any("multi_domain" in r for r in decision.reasons), \
        "single-domain should have no multi_domain reason"


def test_multi_domain_short_stays_standard() -> None:
    """Multi-domain but SHORT (<120 chars, D3 false) → standard.
    Proves the two-gate AND requirement: D2 alone cannot promote without D3."""
    prompt = "Fix the legal contract and the financial revenue."
    decision = route_bsl_chat(_request(prompt), _config())
    # D2=true (law: legal,contract >=2; finance: financial,revenue >=2)
    # D3=false (<120 chars)
    # DEEP = (D1 v D2 v D4) AND D3 = (F v T v F) AND F = F → standard
    assert decision.complexity_bucket == "standard", \
        "D2 alone without D3 must NOT promote to deep (two-gate AND)"
    # Verify D2 flag *is* in reasons — it fired but was gated by D3
    assert any("multi_domain" in r for r in decision.reasons), \
        "multi_domain reason should be present (D2 fired, gated by D3)"


def test_d2_wiring_does_not_break_empty_config_path() -> None:
    """Regression: D2 wiring must not affect empty-config paths."""
    # Empty config → no overrides, no global_last_fallback → empty model
    decision = route_bsl_chat(
        _request("Analyze the legal contract liability and the financial revenue."),
        {},  # completely empty config
    )
    assert decision.selected_model == ""
    assert decision.source == "unresolved"
    assert decision.fail_open is False
    # Classification still works even with empty config
    assert decision.category in ("law", "finance"), \
        "classification still works even with empty config"
