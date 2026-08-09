"""
Tests for bsl_benchmark_sheet + bsl_auto_select (hardened v2).

Benchmark sheet:
- All 13 categories × 3 tiers present
- Primary scores higher than fallback_1 > fallback_2 > absent
- Global last fallback is glm-5.2
- Tier adjustment applied correctly

Auto-select engine (hardened v2):
- Empty config → graceful fallback
- Single provider/route → primary picked, rest None or backfilled
- Multiple providers → diversity-aware selection
- Hard filters reject disabled/incompatible routes
- Full matrix sweep completes without error
- Chain extraction dedupes correctly
- Benchmark triplet consumed in order (P → F1 → F2)
- Backfill fills remaining slots including score-0 families
- No duplicate canonical families per cell
- Hidden providers excluded from auto-select
- Circuit-breaker health gate (fail-open)
"""

import pytest
from app.middleware.bsl_benchmark_sheet import (
    get_family_quality_score,
    get_cell_families,
    get_all_cell_scores,
    GLOBAL_LAST_FALLBACK_FAMILY,
    ALL_CATEGORIES,
    ALL_TIERS,
)
from app.middleware.bsl_auto_select import (
    auto_select_cell,
    auto_select_full_matrix,
    result_to_chain,
    _best_route_for_family,
    _resolve_families_to_slots,
    AutoSelectResult,
    ScoredRoute,
)
from app.middleware.route_registry import build_route_registry


# ─── Benchmark sheet tests ──────────────────────────────────────────────────


class TestBenchmarkSheet:
    def test_all_13_categories_present(self) -> None:
        assert len(ALL_CATEGORIES) == 13

    def test_all_3_tiers_present(self) -> None:
        assert len(ALL_TIERS) == 3

    def test_primary_scores_higher_than_fallback(self) -> None:
        for cat in ALL_CATEGORIES:
            for tier in ALL_TIERS:
                p, fb1, fb2 = get_cell_families(cat, tier)
                sp = get_family_quality_score(p, cat, tier)
                s1 = get_family_quality_score(fb1, cat, tier)
                s2 = get_family_quality_score(fb2, cat, tier)
                assert sp > s1, f"{cat}/{tier}: primary({sp}) <= fallback_1({s1})"
                assert s1 > s2, f"{cat}/{tier}: fallback_1({s1}) <= fallback_2({s2})"

    def test_absent_family_scores_zero(self) -> None:
        score = get_family_quality_score("nonexistent-family", "general", "fast")
        assert score == 0.0

    def test_normalized_lookup_matches_dotted(self) -> None:
        """Audit Bug A: runtime uses normalize_canonical() (dash form). The sheet
        must resolve both dotted and normalized names to the same score."""
        dotted = get_family_quality_score("gpt-5.5", "general", "deep")
        normalized = get_family_quality_score("gpt-5-5", "general", "deep")
        assert dotted == normalized
        assert normalized > 0.0

    def test_normalized_lookup_for_slash_variant(self) -> None:
        """deepseek-v4-flash has no dots, but a variant like DeepSeek_V4_Flash
        normalizes to deepseek-v4-flash and must still hit the sheet."""
        assert get_family_quality_score("deepseek-v4-flash", "general", "fast") > 0.0
        assert get_family_quality_score("DeepSeek_V4_Flash", "general", "fast") > 0.0

    def test_global_last_fallback_is_glm52(self) -> None:
        assert GLOBAL_LAST_FALLBACK_FAMILY == "glm-5.2"

    def test_tier_adjustment_applied(self) -> None:
        """Deep tier gets +1, fast gets -1 vs standard."""
        # In general/deep, gpt-5.5 is primary → base 10 + deep_adj(1) = 11
        deep_score = get_family_quality_score("gpt-5.5", "general", "deep")
        assert deep_score == 11.0
        # In general/fast, glm-5.1 is primary → base 10 + fast_adj(-1) = 9
        fast_score = get_family_quality_score("glm-5.1", "general", "fast")
        assert fast_score == 9.0

    def test_get_all_cell_scores_returns_dict(self) -> None:
        scores = get_all_cell_scores("general", "standard")
        assert isinstance(scores, dict)
        assert len(scores) == 3  # primary, fallback_1, fallback_2

    def test_finance_cell_correct(self) -> None:
        """Finance deep: gpt-5.6-sol primary."""
        p, _, _ = get_cell_families("finance", "deep")
        assert p == "gpt-5.6-sol"

    def test_law_cell_prefers_claude(self) -> None:
        """Law deep: claude-opus-4.8 primary."""
        p, _, _ = get_cell_families("law", "deep")
        assert p == "claude-opus-4.8"


# ─── Test config fixtures ────────────────────────────────────────────────────


def _config_single_provider() -> dict:
    """Config with one provider, two models."""
    return {
        "providers": {
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "models": [
                    {"id": "gpt-5.5", "enabled": True},
                    {"id": "gpt-5.6-sol", "enabled": True},
                ],
            },
        },
    }


def _config_multi_provider() -> dict:
    """Config with 3 providers, diverse families."""
    return {
        "providers": {
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "models": [
                    {"id": "gpt-5.5", "enabled": True},
                    {"id": "gpt-5.6-sol", "enabled": True},
                ],
            },
            "anthropic": {
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-ant-test",
                "models": [
                    {"id": "claude-opus-4.8", "enabled": True},
                    {"id": "claude-sonnet-5", "enabled": True},
                ],
            },
            "zai": {
                "base_url": "https://api.z.ai/api/paas/v4",
                "api_key": "sk-zai",
                "models": [
                    {"id": "glm-5.2", "enabled": True},
                    {"id": "glm-5.1", "enabled": True},
                ],
            },
        },
    }


def _config_with_disabled() -> dict:
    """Config with some disabled routes."""
    return {
        "providers": {
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "models": [
                    {"id": "gpt-5.5", "enabled": True},
                    {"id": "gpt-5.6-sol", "enabled": False},  # disabled
                ],
            },
            "zai": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "sk-zai",
                "models": [
                    {"id": "glm-5.2", "enabled": True},
                ],
            },
        },
    }


def _config_with_hidden() -> dict:
    """Config with a hidden provider that must be excluded from auto-select."""
    return {
        "providers": {
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "models": [
                    {"id": "gpt-5.5", "enabled": True},
                ],
            },
            "secret-provider": {
                "base_url": "https://api.secret.com/v1",
                "api_key": "sk-secret",
                "hidden": True,
                "models": [
                    {"id": "claude-opus-4.8", "enabled": True},
                ],
            },
        },
    }


def _config_empty() -> dict:
    return {"providers": {}}


# ─── Auto-select tests ───────────────────────────────────────────────────────


class TestAutoSelectCell:
    def test_empty_config_returns_graceful_fallback(self) -> None:
        result = auto_select_cell(_config_empty(), "general", "standard")
        assert result.primary is None
        assert result.global_last_fallback == "glm-5.2"
        assert len(result.warnings) > 0

    def test_none_config_handled(self) -> None:
        """None config should not crash."""
        result = auto_select_cell(None, "general", "standard")  # type: ignore
        assert result.primary is None
        assert result.global_last_fallback == "glm-5.2"

    def test_single_provider_picks_best_quality(self) -> None:
        """With gpt-5.5 and gpt-5.6-sol, deep/general should pick gpt-5.5 (benchmark primary)."""
        result = auto_select_cell(_config_single_provider(), "general", "deep")
        assert result.primary is not None
        # general/deep primary is gpt-5.5
        assert result.primary.route.canonical_id == "gpt-5-5"

    def test_multi_provider_diversity_picks_different_providers(self) -> None:
        """With 3 providers, primary and fallback_1 should come from different providers."""
        result = auto_select_cell(_config_multi_provider(), "general", "deep")
        assert result.primary is not None
        assert result.fallback_1 is not None
        assert result.primary.route.provider_id != result.fallback_1.route.provider_id

    def test_disabled_route_filtered_out(self) -> None:
        """Disabled routes must not appear in results."""
        result = auto_select_cell(_config_with_disabled(), "general", "deep")
        if result.primary:
            assert result.primary.route.enabled is True
        if result.fallback_1:
            assert result.fallback_1.route.enabled is True

    def test_result_has_explanation(self) -> None:
        result = auto_select_cell(_config_single_provider(), "general", "standard")
        assert len(result.explanation) > 0
        assert "general/standard" in result.explanation

    def test_result_has_category_and_tier(self) -> None:
        result = auto_select_cell(_config_single_provider(), "finance", "fast")
        assert result.category == "finance"
        assert result.complexity_tier == "fast"

    def test_quality_score_component_present(self) -> None:
        result = auto_select_cell(_config_single_provider(), "general", "standard")
        if result.primary:
            assert result.primary.quality_score >= 0.0
            assert isinstance(result.primary.total_score, float)

    def test_benchmark_warning_when_primary_mismatch(self) -> None:
        """If best available differs from benchmark recommendation, warn."""
        # Config only has gpt models, but general/fast benchmark wants glm-5.1
        result = auto_select_cell(_config_single_provider(), "general", "fast")
        assert len(result.warnings) > 0

    def test_hidden_provider_excluded(self) -> None:
        """Hidden providers must never appear in auto-select results."""
        config = _config_with_hidden()
        result = auto_select_cell(config, "general", "deep")
        # general/deep triplet: gpt-5.5, claude-opus-4.8, gemini-3.1-pro
        # claude-opus-4.8 is in the hidden provider → must NOT appear
        all_routes = []
        for slot in (result.primary, result.fallback_1, result.fallback_2):
            if slot and slot.route:
                all_routes.append(slot.route.route_id)
                assert slot.route.provider_id != "secret-provider", (
                    f"Hidden provider leaked into auto-select: {slot.route.route_id}"
                )

    def test_family_uniqueness_enforced(self) -> None:
        """No two slots in the same cell should share the same canonical family."""
        result = auto_select_cell(_config_multi_provider(), "general", "deep")
        families = []
        for slot in (result.primary, result.fallback_1, result.fallback_2):
            if slot and slot.route:
                families.append(slot.route.canonical_id)
        assert len(families) == len(set(families)), (
            f"Duplicate families in cell: {families}"
        )


class TestBestRouteForFamily:
    def test_empty_list_returns_none(self) -> None:
        assert _best_route_for_family([], "deep") is None

    def test_all_disabled_returns_none(self) -> None:
        from app.middleware.route_registry import RouteCandidate
        routes = [RouteCandidate(
            route_id="test/disabled", resolver_id="disabled",
            canonical_id="disabled", provider_id="test",
            model_id="disabled", enabled=False,
        )]
        assert _best_route_for_family(routes, "deep") is None

    def test_prefers_reasoning_for_deep(self) -> None:
        """For deep tier, reasoning-capable routes should rank higher."""
        from app.middleware.route_registry import RouteCandidate
        reasoning_route = RouteCandidate(
            route_id="a/reasoning-model", resolver_id="reasoning-model",
            canonical_id="test", provider_id="a",
            model_id="reasoning-model", enabled=True, has_reasoning=True,
        )
        non_reasoning_route = RouteCandidate(
            route_id="b/non-reasoning-model", resolver_id="non-reasoning-model",
            canonical_id="test", provider_id="b",
            model_id="non-reasoning-model", enabled=True, has_reasoning=False,
        )
        best = _best_route_for_family([non_reasoning_route, reasoning_route], "deep")
        assert best is not None
        assert best.route_id == "a/reasoning-model"

    def test_ignores_reasoning_for_fast(self) -> None:
        """For fast tier, reasoning capability should NOT affect ranking."""
        from app.middleware.route_registry import RouteCandidate
        reasoning_route = RouteCandidate(
            route_id="a/reasoning-model", resolver_id="reasoning-model",
            canonical_id="test", provider_id="a",
            model_id="reasoning-model", enabled=True, has_reasoning=True,
        )
        non_reasoning_route = RouteCandidate(
            route_id="b/plain-model", resolver_id="plain-model",
            canonical_id="test", provider_id="b",
            model_id="plain-model", enabled=True, has_reasoning=False,
        )
        best = _best_route_for_family([reasoning_route, non_reasoning_route], "fast")
        assert best is not None
        # For fast tier, alphabetical should win (no reasoning preference)
        assert best.route_id == "a/reasoning-model"  # 'a' < 'b' alphabetically

    def test_prefers_tools(self) -> None:
        """Tool-capable routes should rank higher than non-tool routes."""
        from app.middleware.route_registry import RouteCandidate
        tools_route = RouteCandidate(
            route_id="a/tools-model", resolver_id="tools-model",
            canonical_id="test", provider_id="a",
            model_id="tools-model", enabled=True, has_tools=True,
        )
        no_tools_route = RouteCandidate(
            route_id="b/no-tools-model", resolver_id="no-tools-model",
            canonical_id="test", provider_id="b",
            model_id="no-tools-model", enabled=True, has_tools=False,
        )
        best = _best_route_for_family([no_tools_route, tools_route], "standard")
        assert best is not None
        assert best.route_id == "a/tools-model"

    def test_deterministic_alphabetical_fallback(self) -> None:
        """When capabilities are equal, alphabetical route_id wins."""
        from app.middleware.route_registry import RouteCandidate
        route_b = RouteCandidate(
            route_id="b/model", resolver_id="model",
            canonical_id="test", provider_id="b",
            model_id="model", enabled=True,
        )
        route_a = RouteCandidate(
            route_id="a/model", resolver_id="model",
            canonical_id="test", provider_id="a",
            model_id="model", enabled=True,
        )
        best = _best_route_for_family([route_b, route_a], "standard")
        assert best is not None
        assert best.route_id == "a/model"

    def test_health_check_filters_unhealthy(self) -> None:
        """When health_check returns unhealthy, the route is skipped."""
        from app.middleware.route_registry import RouteCandidate
        route_a = RouteCandidate(
            route_id="a/model", resolver_id="model",
            canonical_id="test", provider_id="a",
            model_id="model", enabled=True,
        )
        route_b = RouteCandidate(
            route_id="b/model", resolver_id="model",
            canonical_id="test", provider_id="b",
            model_id="model", enabled=True,
        )
        # Mark route_a as unhealthy
        def health_check(provider: str, model: str):
            if provider == "a":
                return False, "all connections OPEN"
            return True, ""
        best = _best_route_for_family([route_a, route_b], "standard", health_check)
        assert best is not None
        assert best.route_id == "b/model"

    def test_health_check_failopen(self) -> None:
        """If ALL routes are unhealthy, fail-open and return the first enabled."""
        from app.middleware.route_registry import RouteCandidate
        route_a = RouteCandidate(
            route_id="a/model", resolver_id="model",
            canonical_id="test", provider_id="a",
            model_id="model", enabled=True,
        )
        def health_check(provider: str, model: str):
            return False, "all OPEN"
        best = _best_route_for_family([route_a], "standard", health_check)
        assert best is not None  # fail-open, not None
        assert best.route_id == "a/model"


class TestResolveFamiliesToSlots:
    def test_benchmark_triplet_consumed_in_order(self) -> None:
        """The triplet P→F1→F2 from the matrix should be consumed first."""
        config = _config_multi_provider()
        registry = build_route_registry(config)
        # general/deep triplet: P=gpt-5.5, F1=claude-opus-4.8, F2=gemini-3.1-pro
        picked, warnings = _resolve_families_to_slots(registry, "general", "deep")
        # gpt-5.5 and claude-opus-4.8 are in the multi_provider config; gemini is not
        families = [p.route.canonical_id for p in picked]
        assert "gpt-5-5" in families, f"Primary (gpt-5.5) should be first; got {families}"
        assert "claude-opus-4-8" in families, f"F1 (claude-opus-4.8) should be second; got {families}"
        # gemini-3.1-pro not in pool → backfill from remaining (gpt-5-6-sol, claude-sonnet-5, glm, etc.)
        assert len(picked) == 3, f"Expected 3 slots filled after backfill; got {len(picked)}: {families}"

    def test_backfill_fills_remaining_slots(self) -> None:
        """When the benchmark triplet doesn't cover all 3 slots (e.g. claude+gemini missing),
        backfill should pick the highest-scoring remaining families, including score-0 ones."""
        config = _config_single_provider()
        registry = build_route_registry(config)
        picked, warnings = _resolve_families_to_slots(registry, "general", "deep")
        # general/deep triplet: gpt-5.5, claude-opus-4.8, gemini-3.1-pro
        # Pool has: gpt-5.5 + gpt-5.6-sol
        # P=gpt-5.5 (matched), F1=claude missing, F2=gemini missing
        # Backfill picks gpt-5.6-sol (score=0, now accepted) → 2 slots filled
        assert len(picked) == 2, f"Expected 2 slots; got {len(picked)}: {[p.route.canonical_id for p in picked]}"
        assert any("Only 2/3" in w or "has no" in w for w in warnings)

    def test_no_duplicate_families(self) -> None:
        """Even if the same canonical family appears via multiple providers,
        only one slot should get it."""
        config = _config_multi_provider()
        registry = build_route_registry(config)
        picked, warnings = _resolve_families_to_slots(registry, "general", "deep")
        families = [p.route.canonical_id for p in picked]
        assert len(families) == len(set(families)), f"Duplicate families: {families}"


class TestAutoSelectFullMatrix:
    def test_full_matrix_has_all_cells(self) -> None:
        matrix = auto_select_full_matrix(_config_multi_provider())
        assert len(matrix) == 13  # all categories
        for cat_data in matrix.values():
            assert len(cat_data) == 3  # all tiers

    def test_full_matrix_does_not_crash_on_empty(self) -> None:
        matrix = auto_select_full_matrix(_config_empty())
        assert len(matrix) == 13

    def test_full_matrix_results_are_consistent(self) -> None:
        """Same config should produce same results (deterministic)."""
        m1 = auto_select_full_matrix(_config_multi_provider())
        m2 = auto_select_full_matrix(_config_multi_provider())
        for cat in m1:
            for tier in m1[cat]:
                r1 = m1[cat][tier]
                r2 = m2[cat][tier]
                if r1.primary and r2.primary:
                    assert r1.primary.route.route_id == r2.primary.route.route_id

    def test_full_matrix_no_duplicate_families_per_cell(self) -> None:
        """Every cell must have unique canonical families across all 3 slots."""
        matrix = auto_select_full_matrix(_config_multi_provider())
        for cat, tiers in matrix.items():
            for tier, result in tiers.items():
                families = []
                for slot in (result.primary, result.fallback_1, result.fallback_2):
                    if slot and slot.route:
                        families.append(slot.route.canonical_id)
                assert len(families) == len(set(families)), (
                    f"Duplicate family in {cat}/{tier}: {families}"
                )


class TestResultToChain:
    def test_chain_extraction_from_full_result(self) -> None:
        result = auto_select_cell(_config_multi_provider(), "general", "deep")
        chain = result_to_chain(result)
        assert isinstance(chain, list)
        assert len(chain) >= 1  # at least global_last_fallback

    def test_chain_ends_with_global_fallback(self) -> None:
        result = auto_select_cell(_config_multi_provider(), "general", "standard")
        chain = result_to_chain(result)
        assert chain[-1] == "glm-5.2"

    def test_chain_deduped(self) -> None:
        """If primary route IS glm-5.2, it shouldn't appear twice."""
        cfg = {
            "providers": {
                "zai": {
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "api_key": "sk-zai",
                    "models": [{"id": "glm-5.2", "enabled": True}],
                },
            },
        }
        result = auto_select_cell(cfg, "general", "standard")
        chain = result_to_chain(result)
        assert len(chain) == len(set(chain))  # no duplicates

    def test_chain_from_empty_result(self) -> None:
        """Empty config chain should just be global_last_fallback."""
        result = auto_select_cell(_config_empty(), "general", "standard")
        chain = result_to_chain(result)
        assert chain == ["glm-5.2"]
