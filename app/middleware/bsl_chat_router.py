"""
Middleware.bsl_chat_router — BSL-Chat combo alias selector.

Deterministically maps a ``model=bsl-chat`` request to a configured combo alias
based on category classification + task complexity. Pure, stateless, fail-open.
"""

from dataclasses import dataclass, field
from typing import List

from app.middleware.category_classifier import (
    classify_request_category,
    CategoryDecision,
)
from app.middleware.task_complexity import (
    estimate_request_complexity,
    ComplexityDecision,
    COMPLEXITY_TRIVIAL,
    COMPLEXITY_STANDARD,
    COMPLEXITY_DEEP,
)
from app.models import ChatCompletionRequest

from app.middleware.bsl_router_utils import _extract_route, _request_has_vision


# ─── Constants ──────────────────────────────────────────────────────────────

BUCKET_FAST = "fast"
BUCKET_STANDARD = "standard"
BUCKET_DEEP = "deep"


def _get_bsl_cfg(config: dict) -> dict:
    """Read bsl_chat config with canonical-then-legacy compatibility.

    Canonical path (future): ``bsl_models.bsl_chat``
    Legacy path (current bootstrap): ``bsl_chat``

    Returns ``{}`` on None/non-dict to keep callers safe. This is the
    SOLE PLACE where the schema migration shim lives — all downstream code
    reads from the returned dict without caring which root key was used.
    """
    if not isinstance(config, dict):
        return {}
    bsl_models = config.get("bsl_models")
    if isinstance(bsl_models, dict):
        canonical = bsl_models.get("bsl_chat")
        if isinstance(canonical, dict):
            return canonical
    legacy = config.get("bsl_chat")
    if isinstance(legacy, dict):
        return legacy
    return {}


# ─── Decision dataclass ─────────────────────────────────────────────────────

@dataclass
class BSLChatRouteDecision:
    selected_model: str
    category: str
    category_confidence: float
    complexity_level: str
    complexity_bucket: str
    source: str
    reasons: List[str] = field(default_factory=list)
    fail_open: bool = False
    fallback_chain: List[str] = field(default_factory=list)





# ─── Helpers ─────────────────────────────────────────────────────────────────

def _complexity_to_bucket(complexity_level: str) -> str:
    """Map a task-complexity level to a bsl-chat bucket key (1:1)."""
    if complexity_level == COMPLEXITY_TRIVIAL:
        return BUCKET_FAST
    if complexity_level == COMPLEXITY_STANDARD:
        return BUCKET_STANDARD
    if complexity_level == COMPLEXITY_DEEP:
        return BUCKET_DEEP
    return BUCKET_STANDARD


def _select_model(
    config: dict,
    category_decision: CategoryDecision,
    complexity_decision: ComplexityDecision,
) -> BSLChatRouteDecision:
    """Pure lookup logic. Assumes valid config structure.

    Precedence (no hardcoded safety net — empty = 503):
      0. default_route (complexity override — bypasses entire matrix)
      1. category_override[classified_category][bucket]
      2. category_override["general"][bucket]  (fallback category)
      3. global_last_fallback (always attempted when configured)

    "default_route" is an all-in-one override: when enabled, every request
    routes to the same configured model regardless of category or complexity.
    "global_last_fallback" is the final safety net appended to every chain.
    These are DISTINCT concepts — default_route bypasses the matrix;
    global_last_fallback catches failures at the end of the chain.
    Global Last Fallback has no disable toggle — it is always active when
    configured, so a stale ``global_last_fallback_enabled: false`` can never
    silently disable the final safety net.
    """
    bsl_cfg = _get_bsl_cfg(config)
    category = category_decision.category
    bucket = _complexity_to_bucket(complexity_decision.level)
    reasons: List[str] = []

    selected = ""
    fallback_chain: List[str] = []
    source = "unresolved"

    # 0. Default route — complexity override (all-in-one bypass).
    #    When enabled, skip the entire category×complexity matrix.
    if bsl_cfg.get("default_route_enabled", False):
        default_route = bsl_cfg.get("default_route")
        if default_route:
            selected, fallback_chain = _extract_route(default_route)
            if selected:
                source = "default_route"
                chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
                reasons.append(f"default_route_override={chain_str}")
                reasons.append(f"category={category} (bypassed)")
                reasons.append(f"complexity={complexity_decision.level} (bypassed)")

    category_overrides = bsl_cfg.get("category_overrides", {}) or {}

    # 1. Category override for the classified category (skip "general" —
    #    it's the fallback category, handled at step 2).
    if not selected and category != "general":
        override_for_category = category_overrides.get(category, {}) or {}
        if isinstance(override_for_category, dict) and override_for_category.get(bucket):
            selected, fallback_chain = _extract_route(override_for_category[bucket])
            source = "category_override"
            chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
            reasons.append(f"category_override[{category}][{bucket}]={chain_str}")
            # Append the General fallback cell (P/F1/F2) so the runtime
            # matches the UI promise: Category → General → Global Last Fallback.
            # The dispatcher already deduplicates the chain, so overlapping
            # route IDs are collapsed automatically.
            general_override = category_overrides.get("general", {}) or {}
            if isinstance(general_override, dict) and general_override.get(bucket):
                general_primary, general_fallbacks = _extract_route(general_override[bucket])
                if general_primary:
                    fallback_chain = fallback_chain + [general_primary] + general_fallbacks

    if not selected:
        # 2. General category fallback.
        general_override = category_overrides.get("general", {}) or {}
        if isinstance(general_override, dict) and general_override.get(bucket):
            selected, fallback_chain = _extract_route(general_override[bucket])
            source = "general_fallback"
            chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
            reasons.append(f"general_fallback[{bucket}]={chain_str}")
        else:
            # 3. Global last fallback — always attempted when configured.
            #    Global Last Fallback has no disable toggle; a stale
            #    ``global_last_fallback_enabled: false`` must NOT prevent it.
            global_fallback = bsl_cfg.get("global_last_fallback")
            if global_fallback:
                selected, fallback_chain = _extract_route(global_fallback)
                source = "global_last_fallback"
                reasons.append(f"global_last_fallback={selected}")
            else:
                reasons.append("no global_last_fallback configured")

    return BSLChatRouteDecision(
        selected_model=selected,
        category=category,
        category_confidence=category_decision.confidence,
        complexity_level=complexity_decision.level,
        complexity_bucket=bucket,
        source=source,
        reasons=reasons,
        fail_open=False,
        fallback_chain=fallback_chain,
    )


def _fail_open(
    category: str = "general",
    complexity_level: str = COMPLEXITY_STANDARD,
    complexity_bucket: str = BUCKET_STANDARD,
    reason: str = "exception",
) -> BSLChatRouteDecision:
    """Return a fail-open decision with no selected model.

    The caller (dispatch) must handle the empty selected_model by
    returning a 503 NoHealthyModelError.
    """
    return BSLChatRouteDecision(
        selected_model="",
        category=category,
        category_confidence=0.0,
        complexity_level=complexity_level,
        complexity_bucket=complexity_bucket,
        source="fail_open",
        reasons=[reason],
        fail_open=True,
    )


# ─── Main API ─────────────────────────────────────────────────────────────────

def route_bsl_chat(request: ChatCompletionRequest, config: dict) -> BSLChatRouteDecision:
    """Route a ``model=bsl-chat`` request to a combo alias.

    The returned ``selected_model`` is an alias string (e.g. ``coder-2``) that
    should be fed into the existing combo/alias/provider resolver in ``main.py``.

    Fail-open: any exception returns ``coder-2`` with ``fail_open=True``.
    """
    try:
        # Classify and estimate complexity so logs/tests are stable.
        category_decision = classify_request_category(request)
        # Wire the Phase 2A category score vector into the D2 multi-domain detector.
        # Both functions score on the SAME extract_current_intent basis, so this
        # does not widen the injection surface. category_scores defaults to {} on
        # the classifier's empty-text path, which _detect_multidomain treats as no-op.
        complexity_decision = estimate_request_complexity(
            request, category_scores=category_decision.scores
        )

        # ALWAYS ON (user directive 2026-08-06): BSL-Chat smart routing is the
        # product's core routing logic. It no longer honors the
        # ``bsl_models.bsl_chat.enabled`` or ``tools.bsl_chat_router`` flags.
        # The admin-facing blacksand-chat toggle controls ONLY catalog
        # visibility (/v1/models), never routing behavior. With an empty or
        # missing matrix, _select_model resolves to "" and the dispatcher
        # returns 503 - the same behavior as the old disabled path, just ungated.

        # ── Vision pre-flight (user directive 2026-08-06): if the request ──
        # contains images, vision-capable models go FIRST (prepended to the
        # chat chain). If all vision routes fail recoverably, the dispatcher
        # advances to the chat route, which self-answers.
        if _request_has_vision(request):
            try:
                from app.middleware.bsl_router_utils import select_vision_route

                vision_primary, vision_fallbacks = select_vision_route(config)
                if vision_primary:
                    chat_decision = _select_model(config, category_decision, complexity_decision)
                    agent_chain = []
                    if chat_decision.selected_model:
                        agent_chain = [chat_decision.selected_model] + chat_decision.fallback_chain
                    _seen = set()
                    fb_chain = [e for e in (vision_fallbacks + agent_chain)
                                if e and not (e in _seen or _seen.add(e))]
                    return BSLChatRouteDecision(
                        selected_model=vision_primary,
                        category=category_decision.category,
                        category_confidence=category_decision.confidence,
                        complexity_level=complexity_decision.level,
                        complexity_bucket=_complexity_to_bucket(complexity_decision.level),
                        source="vision_preflight",
                        reasons=[
                            "vision_preflight: request contains images",
                            f"vision chain: {vision_primary} → {fb_chain}",
                        ],
                        fail_open=False,
                        fallback_chain=fb_chain,
                    )
            except Exception as _e:
                print(f"[blacksand-chat] vision pre-flight failed: {_e}", flush=True)

        decision = _select_model(config, category_decision, complexity_decision)
        # Observability: surface D2 multi-domain detection when it fired.
        if complexity_decision.feature_vector.get("D2_multidomain"):
            decision.reasons.append(
                f"multi_domain (runner_up={category_decision.runner_up})"
            )
        return decision
    except Exception as exc:
        return _fail_open(reason=f"exception: {exc}")
