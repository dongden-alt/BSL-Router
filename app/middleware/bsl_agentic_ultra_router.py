"""
BSL-Agentic-Ultra balanced router.

Routing contract copied from Blacksand Code's balanced tier:
Scout classification -> deterministic role selection -> one lead route with
transport fallbacks. Orchestration decisions belong to the phase loop; model
fallbacks never represent agent phases.
"""

from dataclasses import dataclass, field
from typing import List

from app.middleware.coding_category_classifier import (
    CATEGORY_GENERAL,
    CodingCategoryDecision,
    classify_coding_request_category,
)
from app.models import ChatCompletionRequest
from app.middleware.bsl_router_utils import (
    _extract_route,
    _request_has_vision,
    resolve_agent_route,
)

AGENTIC_ULTRA_DEPTH = "balanced"


def _get_bsl_agentic_ultra_cfg(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}
    models = config.get("bsl_models")
    if isinstance(models, dict) and isinstance(models.get("bsl_agentic_ultra"), dict):
        return models["bsl_agentic_ultra"]
    legacy = config.get("bsl_agentic_ultra")
    return legacy if isinstance(legacy, dict) else {}


@dataclass
class BSLAgenticUltraDecision:
    selected_model: str = ""
    category: str = CATEGORY_GENERAL
    category_confidence: float = 0.0
    complexity_level: str = "standard"
    consulted: bool = False  # compatibility field; balanced has no consult matrix
    depth: str = AGENTIC_ULTRA_DEPTH
    source: str = "unresolved"
    reasons: List[str] = field(default_factory=list)
    fail_open: bool = False
    fallback_chain: List[str] = field(default_factory=list)


def _select_model(
    config: dict, category_decision: CodingCategoryDecision
) -> BSLAgenticUltraDecision:
    """Select the current deterministic phase's role route.

    Fallbacks are provider/transport retries only. No consult route, model
    prepend, or complexity matrix exists in Blacksand balanced mode.
    """
    cfg = _get_bsl_agentic_ultra_cfg(config)
    category = category_decision.category
    routes = cfg.get("agent_routes", {}) or {}
    selected = ""
    fallback_chain: List[str] = []
    reasons: List[str] = []
    source = "unresolved"

    if cfg.get("default_route_enabled", False) and cfg.get("default_route"):
        selected, fallback_chain = _extract_route(cfg["default_route"])
        if selected:
            source = "default_route"
            reasons.append("deterministic_default_route")

    if not selected and category != CATEGORY_GENERAL:
        route = resolve_agent_route(routes, category)
        if route:
            selected, fallback_chain = _extract_route(route)
            source = "agent_route"
            reasons.append(f"scout_classification={category}")
            general = routes.get(CATEGORY_GENERAL)
            if general:
                primary, fallbacks = _extract_route(general)
                if primary:
                    fallback_chain += [primary, *fallbacks]

    if not selected:
        general = routes.get(CATEGORY_GENERAL)
        if general:
            selected, fallback_chain = _extract_route(general)
            source = "scout_direct"
            reasons.append("scout_classification=general")
        else:
            global_fallback = cfg.get("global_last_fallback")
            if global_fallback:
                selected, fallback_chain = _extract_route(global_fallback)
                source = "global_last_fallback"
                reasons.append("no_role_route")
            else:
                reasons.append("no_global_last_fallback_configured")

    return BSLAgenticUltraDecision(
        selected_model=selected,
        category=category,
        category_confidence=category_decision.confidence,
        complexity_level="standard",
        consulted=False,
        depth=AGENTIC_ULTRA_DEPTH,
        source=source,
        reasons=reasons,
        fallback_chain=fallback_chain,
    )


def _fail_open(reason: str = "exception") -> BSLAgenticUltraDecision:
    return BSLAgenticUltraDecision(
        source="fail_open", reasons=[reason], fail_open=True, depth=AGENTIC_ULTRA_DEPTH
    )


def route_bsl_agentic_ultra(
    request: ChatCompletionRequest, config: dict
) -> BSLAgenticUltraDecision:
    """Scout-classify, then select the deterministic balanced role route."""
    try:
        cfg = _get_bsl_agentic_ultra_cfg(config)
        category = classify_coding_request_category(request)

        # Vision is input preparation, not a second orchestration model.
        if _request_has_vision(request):
            try:
                from app.middleware.bsl_router_utils import select_vision_route

                vision_primary, vision_fallbacks = select_vision_route(config)
                if vision_primary:
                    base = _select_model(config, category)
                    chain = [base.selected_model, *base.fallback_chain]
                    global_fallback = cfg.get("global_last_fallback")
                    if global_fallback:
                        primary, fallbacks = _extract_route(global_fallback)
                        if primary:
                            chain += [primary, *fallbacks]
                    seen = set()
                    return BSLAgenticUltraDecision(
                        selected_model=vision_primary,
                        category=category.category,
                        category_confidence=category.confidence,
                        source="vision_preflight",
                        reasons=["vision_preflight_before_scout_phase"],
                        fallback_chain=[
                            model
                            for model in [*vision_fallbacks, *chain]
                            if model and not (model in seen or seen.add(model))
                        ],
                    )
            except Exception as exc:
                print(f"[BSLAgenticUltra] vision pre-flight failed: {exc}", flush=True)

        return _select_model(config, category)
    except Exception as exc:
        return _fail_open(f"exception: {exc}")
