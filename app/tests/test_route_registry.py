"""
Tests for route_registry — canonical family ↔ concrete route mapper (P1).

Covers:
- Canonical normalization (suffix stripping, separator normalization)
- Route registry construction from config
- Family lookup with enabled/disabled routes
- Canonical chain resolution (first-match + full-list)
- Coverage report for matrix families
"""

from app.middleware.route_registry import (
    normalize_canonical,
    build_route_registry,
    find_routes_for_family,
    resolve_canonical_chain,
    resolve_full_chain,
    list_available_families,
    coverage_report,
    RouteCandidate,
    FamilyGroup,
)


# ─── Test config helpers ────────────────────────────────────────────────────


def _config_with_providers(*providers: dict) -> dict:
    """Build a config dict from provider specs."""
    result = {"providers": {}}
    for spec in providers:
        pid = spec["id"]
        result["providers"][pid] = {
            "base_url": spec.get("base_url", "https://api.openai.com/v1"),
            "models": spec.get("models", []),
            "connections": spec.get("connections", [{"enabled": True}]),
        }
    return result


def _model(model_id: str, **kwargs) -> dict:
    """Build a model entry."""
    entry = {"id": model_id, "enabled": True}
    entry.update(kwargs)
    return entry


# ─── Canonical normalization tests ──────────────────────────────────────────


def test_normalize_strips_effort_suffixes() -> None:
    assert normalize_canonical("gpt-5.6-sol-max") == "gpt-5-6-sol"
    assert normalize_canonical("gpt-5.6-sol-xhigh") == "gpt-5-6-sol"
    assert normalize_canonical("gpt-5.6-sol-high") == "gpt-5-6-sol"
    assert normalize_canonical("gpt-5.6-sol-low") == "gpt-5-6-sol"
    assert normalize_canonical("gpt-5.6-sol-medium") == "gpt-5-6-sol"


def test_normalize_strips_reasoning_suffixes() -> None:
    assert normalize_canonical("claude-opus-4.8-thinking") == "claude-opus-4-8"
    assert normalize_canonical("gpt-5.6-sol-reasoning") == "gpt-5-6-sol"
    assert normalize_canonical("gpt-5.6-sol-non-reasoning") == "gpt-5-6-sol"
    assert normalize_canonical("glm-5.2-minimal") == "glm-5-2"


def test_normalize_strips_special_suffixes() -> None:
    assert normalize_canonical("gpt-5.6-sol-antigravity") == "gpt-5-6-sol"
    assert normalize_canonical("claude-opus-4.8-preview") == "claude-opus-4-8"
    assert normalize_canonical("gpt-oss-120b") == "gpt-oss-120b"  # oss without dash stays


def test_normalize_strips_date_suffixes() -> None:
    assert normalize_canonical("deepseek-v4-pro-0309") == "deepseek-v4-pro-0309"  # 4-digit stays (version)
    assert normalize_canonical("claude-opus-4.8-2025-01") == "claude-opus-4-8"


def test_normalize_normalizes_separators() -> None:
    """Dots and underscores become dashes."""
    assert normalize_canonical("gpt-5.6-sol") == "gpt-5-6-sol"
    assert normalize_canonical("kimi_k2.7_code") == "kimi-k2-7-code"
    assert normalize_canonical("GLM-5.2") == "glm-5-2"
    assert normalize_canonical("DeepSeek-V4-Flash") == "deepseek-v4-flash"


def test_normalize_handles_empty_and_edge() -> None:
    assert normalize_canonical("") == ""
    assert normalize_canonical("gpt") == "gpt"
    assert normalize_canonical("...") == ""


def test_normalize_idempotent() -> None:
    """Normalizing an already-normalized ID gives the same result."""
    families = ["claude-opus-4-8", "gpt-5-6-sol", "glm-5-2", "deepseek-v4-pro"]
    for f in families:
        assert normalize_canonical(f) == f


# ─── Registry construction tests ────────────────────────────────────────────


def test_build_registry_from_single_provider() -> None:
    config = _config_with_providers({
        "id": "vsllm-a",
        "models": [
            _model("claude-opus-4.8"),
            _model("gpt-5.6-sol"),
            _model("glm-5.2"),
        ],
    })
    registry = build_route_registry(config)
    assert len(registry) == 3
    assert "claude-opus-4-8" in registry
    assert "gpt-5-6-sol" in registry
    assert "glm-5-2" in registry


def test_build_registry_groups_variants_into_family() -> None:
    """Multiple variants with different suffixes collapse to one family."""
    config = _config_with_providers({
        "id": "prov-a",
        "models": [
            _model("gpt-5.6-sol-max"),
            _model("gpt-5.6-sol-high"),
            _model("gpt-5.6-sol-low"),
        ],
    })
    registry = build_route_registry(config)
    # All three collapse to the same canonical family
    assert len(registry) == 1
    family = registry["gpt-5-6-sol"]
    assert len(family.routes) == 3


def test_build_registry_cross_provider_same_family() -> None:
    """Same canonical family from different providers groups together."""
    config = _config_with_providers(
        {"id": "prov-a", "models": [_model("glm-5.2")]},
        {"id": "prov-b", "models": [_model("GLM-5.2-Thinking")]},
        {"id": "prov-c", "models": [_model("glm_5.2")]},
    )
    registry = build_route_registry(config)
    family = registry["glm-5-2"]
    assert len(family.routes) == 3
    providers = {r.provider_id for r in family.routes}
    assert providers == {"prov-a", "prov-b", "prov-c"}


def test_build_registry_skips_empty_config() -> None:
    assert build_route_registry({}) == {}
    assert build_route_registry(None) == {}  # type: ignore
    assert build_route_registry({"providers": {}}) == {}


def test_build_registry_marks_disabled_routes() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [
            _model("glm-5.2", enabled=True),
            _model("gpt-5.5", enabled=False),
        ],
    })
    registry = build_route_registry(config)
    glm_family = registry["glm-5-2"]
    assert len(glm_family.enabled_routes) == 1

    gpt_family = registry["gpt-5-5"]
    assert len(gpt_family.enabled_routes) == 0
    assert not gpt_family.is_available


# ─── Lookup API tests ────────────────────────────────────────────────────────


def test_find_routes_returns_enabled_only_by_default() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [
            _model("claude-opus-4.8"),
            _model("gpt-5.5", enabled=False),
        ],
    })
    registry = build_route_registry(config)
    claude_routes = find_routes_for_family(registry, "claude-opus-4.8")
    assert len(claude_routes) == 1

    gpt_routes = find_routes_for_family(registry, "gpt-5.5")
    assert len(gpt_routes) == 0  # disabled, filtered out

    gpt_all = find_routes_for_family(registry, "gpt-5.5", enabled_only=False)
    assert len(gpt_all) == 1


def test_find_routes_normalizes_query() -> None:
    """Query with dots/dashes/underscores all match the same family."""
    config = _config_with_providers({
        "id": "prov-a",
        "models": [_model("glm-5.2")],
    })
    registry = build_route_registry(config)
    # All these queries should find the same family
    for query in ["glm-5.2", "glm_5_2", "GLM-5.2", "glm-5-2"]:
        routes = find_routes_for_family(registry, query)
        assert len(routes) == 1, f"Query '{query}' should find glm-5-2"


def test_find_routes_returns_empty_for_missing_family() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [_model("glm-5.2")],
    })
    registry = build_route_registry(config)
    assert find_routes_for_family(registry, "nonexistent-model") == []


# ─── Chain resolution tests ─────────────────────────────────────────────────


def test_resolve_canonical_chain_first_match() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [_model("gpt-5.6-sol"), _model("glm-5.2")],
    })
    registry = build_route_registry(config)
    chain = ["claude-opus-4.8", "gpt-5.6-sol", "gemini-3.1-pro"]
    result = resolve_canonical_chain(registry, chain)
    assert result is not None
    canonical, provider, model = result
    assert canonical == "gpt-5-6-sol"
    assert provider == "prov-a"
    assert model == "gpt-5.6-sol"


def test_resolve_canonical_chain_returns_none_if_all_missing() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [_model("glm-5.2")],
    })
    registry = build_route_registry(config)
    chain = ["claude-opus-4.8", "gpt-5.6-sol"]
    assert resolve_canonical_chain(registry, chain) is None


def test_resolve_full_chain_returns_all_routes() -> None:
    config = _config_with_providers(
        {"id": "prov-a", "models": [_model("gpt-5.6-sol")]},
        {"id": "prov-b", "models": [_model("gpt-5.6-sol-max"), _model("glm-5.2")]},
    )
    registry = build_route_registry(config)
    chain = ["gpt-5.6-sol", "glm-5.2"]
    result = resolve_full_chain(registry, chain)
    # gpt-5.6-sol from prov-a, gpt-5.6-sol-max from prov-b, glm-5.2 from prov-b
    assert len(result) == 3
    assert result[0][1] == "prov-a"  # first gpt-5-6-sol route
    assert result[1][1] == "prov-b"  # second gpt-5-6-sol variant
    assert result[2][0] == "glm-5-2"  # glm family


# ─── Coverage report tests ──────────────────────────────────────────────────


def test_coverage_report_identifies_gaps() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [_model("glm-5.2"), _model("gpt-5.5")],
    })
    registry = build_route_registry(config)
    required = ["glm-5.2", "gpt-5.5", "claude-opus-4.8", "gemini-3.1-pro"]
    report = coverage_report(registry, required)
    assert report["glm-5-2"] is True
    assert report["gpt-5-5"] is True
    assert report["claude-opus-4-8"] is False
    assert report["gemini-3-1-pro"] is False


def test_list_available_families_sorted() -> None:
    config = _config_with_providers({
        "id": "prov-a",
        "models": [_model("zeta-1"), _model("alpha-2"), _model("mid-3")],
    })
    registry = build_route_registry(config)
    families = list_available_families(registry)
    assert families == ["alpha-2", "mid-3", "zeta-1"]


# ─── Protocol inference tests ───────────────────────────────────────────────


def test_protocol_inference_anthropic() -> None:
    config = _config_with_providers({
        "id": "anthropic-prov",
        "base_url": "https://api.anthropic.com/v1",
        "models": [_model("claude-opus-4.8")],
    })
    registry = build_route_registry(config)
    route = registry["claude-opus-4-8"].routes[0]
    assert route.protocol == "anthropic"


def test_protocol_inference_gemini() -> None:
    config = _config_with_providers({
        "id": "google-prov",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "models": [_model("gemini-3.1-pro")],
    })
    registry = build_route_registry(config)
    route = registry["gemini-3-1-pro"].routes[0]
    assert route.protocol == "gemini"
