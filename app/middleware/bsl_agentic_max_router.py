"""
Middleware.bsl_agentic_max_router — BSL-Agentic-Max Multi-Domain Router (L3)

BSL-Agentic-Max is the multi-domain fusion router for Openclaw / Hermes apps,
where a single request may be a coding task, a general chat task, or mixed.
It fuses the two existing routing brains:

  - Coding brain  -> coding_category_classifier + agent_routes (bsl-agentic)
  - Chat brain    -> category_classifier + chat_routes (bsl-chat 13x3 matrix)

Flow:
  1. Dual classification: run BOTH classifiers on the same request.
  2. Domain detection via a configurable merge_strategy:
       coding_priority      -> any coding signal wins
       chat_priority        -> any chat signal wins
       confidence_weighted  -> higher-confidence domain wins (default)
       dual_route           -> resolve both routes, pick higher-confidence one
  3. Route through the winning domain's matrix; append the losing domain's
     general fallback + global_last_fallback to the chain.

Depth = balanced. LOCKED. No structural phase expansion / member spawning /
quality-gate rounds — those are exclusively Blacksand Code's "deep" tier.
Max produces ONE response; "multi-domain" is routing intelligence only.

ALWAYS ON (user directive 2026-08-06, same rule as the other bsl routers):
  Routing no longer honors the ``enabled`` / ``tools.bsl_agentic_max_router``
  flags. ``bsl_models.bsl_agentic_max.enabled`` controls ONLY catalog
  visibility (/v1/models), never routing behavior. With an empty matrix
  ``_select_model`` resolves to "" and the dispatcher returns 503 - the same
  observable outcome as the old disabled path, just ungated.

Single source of truth (2026-08-02): the coding/chat matrices are NOT stored
under ``bsl_agentic_max``. Max DERIVES them from the sibling configs so there
is exactly one place to edit each matrix (matches the read-only Max UI):

  - coding matrix  <- ``bsl_models.bsl_agentic.agent_routes``
  - chat matrix    <- ``bsl_models.bsl_chat.category_overrides``

To change a route, edit the Blacksand Agentic or Blacksand Chat config; Max
follows automatically. ``bsl_agentic_max`` itself only stores fusion scalars
(``enabled``, ``merge_strategy``, ``default_route*``, ``global_last_fallback``).
A legacy self-contained ``bsl_agentic_max.agent_routes`` / ``chat_routes``
block, if present, is used only when the sibling matrix is absent.

Config schema (canonical):
    bsl_models:
      bsl_agentic_max:
        enabled: true
        merge_strategy: confidence_weighted
        global_last_fallback: "glm-5.2"
      # derived:
      bsl_agentic:
        agent_routes:    # coding matrix (same shape as bsl_agentic)
          power_coder: { primary: "coder-2", fallback_1: "", fallback_2: "" }
      bsl_chat:
        category_overrides:  # chat 13-category x 3-bucket matrix
          technical: { fast: {...}, standard: {...}, deep: {...} }
"""

from dataclasses import dataclass, field
from typing import List

from app.middleware.coding_category_classifier import (
    classify_coding_request_category,
    CATEGORY_GENERAL as CODING_GENERAL,
)
from app.middleware.category_classifier import (
    classify_request_category,
    CATEGORY_GENERAL as CHAT_GENERAL,
)
from app.middleware.task_complexity import (
    estimate_request_complexity,
    COMPLEXITY_TRIVIAL,
    COMPLEXITY_DEEP,
)
from app.models import ChatCompletionRequest

from app.middleware.bsl_router_utils import _extract_route, _request_has_vision, resolve_agent_route


# ─── Constants ──────────────────────────────────────────────────────────────

# Depth tier is LOCKED to balanced. Deep is reserved for Blacksand Code.
AGENTIC_MAX_DEPTH = "balanced"

# Merge strategies.
MERGE_CODING_PRIORITY = "coding_priority"
MERGE_CHAT_PRIORITY = "chat_priority"
MERGE_CONFIDENCE_WEIGHTED = "confidence_weighted"
MERGE_DUAL_ROUTE = "dual_route"

_DOMAIN_CODING = "coding"
_DOMAIN_CHAT = "chat"

_BUCKET_FAST = "fast"
_BUCKET_STANDARD = "standard"
_BUCKET_DEEP = "deep"


def _get_bsl_agentic_max_cfg(config: dict) -> dict:
    """Read bsl_agentic_max config with canonical-then-legacy compatibility."""
    if not isinstance(config, dict):
        return {}
    bsl_models = config.get("bsl_models")
    if isinstance(bsl_models, dict):
        canonical = bsl_models.get("bsl_agentic_max")
        if isinstance(canonical, dict):
            return canonical
    legacy = config.get("bsl_agentic_max")
    if isinstance(legacy, dict):
        return legacy
    return {}


# ─── Effective config (derive from sibling matrices) ──────────────────────────


def _get_sibling_cfg(config: dict, key: str) -> dict:
    """Read ``bsl_models.<key>`` then legacy top-level ``<key>``; {} on miss."""
    if not isinstance(config, dict):
        return {}
    bsl_models = config.get("bsl_models")
    if isinstance(bsl_models, dict):
        sub = bsl_models.get(key)
        if isinstance(sub, dict):
            return sub
    legacy = config.get(key)
    if isinstance(legacy, dict):
        return legacy
    return {}


def _build_effective_cfg(config: dict) -> dict:
    """Return max's scalars merged with the DERIVED coding/chat matrices.

    Single source of truth: coding agent_routes come from ``bsl_agentic`` and
    chat routes come from ``bsl_chat.category_overrides``. Max's own legacy
    ``agent_routes`` / ``chat_routes`` are a fallback only when the sibling
    matrix is absent. Pure — returns a new dict, never mutates ``config``.
    """
    own = _get_bsl_agentic_max_cfg(config)
    effective = dict(own)

    agentic = _get_sibling_cfg(config, "bsl_agentic")
    derived_agent_routes = agentic.get("agent_routes")
    if derived_agent_routes:
        effective["agent_routes"] = derived_agent_routes

    chat = _get_sibling_cfg(config, "bsl_chat")
    derived_chat_routes = chat.get("category_overrides")
    if derived_chat_routes:
        effective["chat_routes"] = derived_chat_routes

    return effective


# ─── Route extraction ───────────────────────────────────────────────────────


def _complexity_to_bucket(level: str) -> str:
    if level == COMPLEXITY_TRIVIAL:
        return _BUCKET_FAST
    if level == COMPLEXITY_DEEP:
        return _BUCKET_DEEP
    return _BUCKET_STANDARD


# ─── Decision dataclass ─────────────────────────────────────────────────────
@dataclass
class BSLAgenticMaxDecision:
    """Result of bsl-agentic-max route selection."""
    selected_model: str = ""
    domain: str = _DOMAIN_CHAT
    coding_category: str = CODING_GENERAL
    chat_category: str = CHAT_GENERAL
    coding_confidence: float = 0.0
    chat_confidence: float = 0.0
    merge_strategy: str = MERGE_CONFIDENCE_WEIGHTED
    depth: str = AGENTIC_MAX_DEPTH
    source: str = "disabled_default"
    reasons: List[str] = field(default_factory=list)
    fail_open: bool = False
    fallback_chain: List[str] = field(default_factory=list)


# ─── Domain resolution ───────────────────────────────────────────────────────


def _pick_domain(strategy: str, coding_conf: float, chat_conf: float) -> str:
    """Resolve which domain wins under the configured merge strategy.

    A domain with zero confidence has "no signal". Priority strategies only
    win when their domain actually has signal; otherwise the other domain is
    used. confidence_weighted picks the higher-confidence domain, tie -> chat.
    """
    coding_signal = coding_conf > 0.0
    chat_signal = chat_conf > 0.0

    if strategy == MERGE_CODING_PRIORITY:
        if coding_signal:
            return _DOMAIN_CODING
        return _DOMAIN_CHAT
    if strategy == MERGE_CHAT_PRIORITY:
        if chat_signal:
            return _DOMAIN_CHAT
        return _DOMAIN_CODING
    if strategy == MERGE_DUAL_ROUTE:
        # Both routes are resolved by the caller; domain decided by confidence.
        if coding_conf > chat_conf:
            return _DOMAIN_CODING
        return _DOMAIN_CHAT
    # confidence_weighted (default)
    if coding_conf > chat_conf:
        return _DOMAIN_CODING
    return _DOMAIN_CHAT


def _resolve_coding_route(bsl_cfg: dict, category: str) -> tuple:
    """Resolve (selected, fallback_chain) from the coding agent_routes matrix."""
    agent_routes = bsl_cfg.get("agent_routes", {}) or {}
    selected = ""
    fallback_chain: List[str] = []

    if category != CODING_GENERAL:
        # Granular sub-agent keys resolve to their own cell first, then parent.
        cell = resolve_agent_route(agent_routes, category)
        if cell:
            selected, fallback_chain = _extract_route(cell)
            general_cell = agent_routes.get(CODING_GENERAL)
            if general_cell:
                g_primary, g_fallbacks = _extract_route(general_cell)
                if g_primary:
                    fallback_chain = fallback_chain + [g_primary] + g_fallbacks

    if not selected:
        general_cell = agent_routes.get(CODING_GENERAL)
        if general_cell:
            selected, fallback_chain = _extract_route(general_cell)

    return (selected, fallback_chain)


def _resolve_chat_route(bsl_cfg: dict, category: str, bucket: str) -> tuple:
    """Resolve (selected, fallback_chain) from the chat 13x3 chat_routes matrix."""
    chat_routes = bsl_cfg.get("chat_routes", {}) or {}
    selected = ""
    fallback_chain: List[str] = []

    if category != CHAT_GENERAL:
        cat_cell = chat_routes.get(category, {}) or {}
        if isinstance(cat_cell, dict) and cat_cell.get(bucket):
            selected, fallback_chain = _extract_route(cat_cell[bucket])
            general_cell = chat_routes.get(CHAT_GENERAL, {}) or {}
            if isinstance(general_cell, dict) and general_cell.get(bucket):
                g_primary, g_fallbacks = _extract_route(general_cell[bucket])
                if g_primary:
                    fallback_chain = fallback_chain + [g_primary] + g_fallbacks

    if not selected:
        general_cell = chat_routes.get(CHAT_GENERAL, {}) or {}
        if isinstance(general_cell, dict) and general_cell.get(bucket):
            selected, fallback_chain = _extract_route(general_cell[bucket])

    return (selected, fallback_chain)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _select_model(
    config: dict,
    coding_decision,
    chat_decision,
    complexity_level: str,
) -> BSLAgenticMaxDecision:
    """Pure fusion logic. Resolve both domain routes, pick winner, merge chains."""
    # Load coding/chat matrices from bsl_agentic + bsl_chat (single source of
    # truth), falling back to max's own legacy blocks if the siblings are unset.
    bsl_cfg = _build_effective_cfg(config)
    strategy = bsl_cfg.get("merge_strategy", MERGE_CONFIDENCE_WEIGHTED)
    reasons: List[str] = []

    coding_conf = coding_decision.confidence
    chat_conf = chat_decision.confidence
    bucket = _complexity_to_bucket(complexity_level)

    # 0. Default route — all-in-one bypass.
    if bsl_cfg.get("default_route_enabled", False):
        default_route = bsl_cfg.get("default_route")
        if default_route:
            selected, fallback_chain = _extract_route(default_route)
            if selected:
                chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
                reasons.append(f"default_route_override={chain_str}")
                return BSLAgenticMaxDecision(
                    selected_model=selected,
                    domain=_DOMAIN_CHAT,
                    coding_category=coding_decision.category,
                    chat_category=chat_decision.category,
                    coding_confidence=coding_conf,
                    chat_confidence=chat_conf,
                    merge_strategy=strategy,
                    depth=AGENTIC_MAX_DEPTH,
                    source="default_route",
                    reasons=reasons,
                    fail_open=False,
                    fallback_chain=fallback_chain,
                )

    domain = _pick_domain(strategy, coding_conf, chat_conf)

    # Resolve both domain routes (needed for dual_route + chain merging).
    coding_selected, coding_chain = _resolve_coding_route(bsl_cfg, coding_decision.category)
    chat_selected, chat_chain = _resolve_chat_route(bsl_cfg, chat_decision.category, bucket)

    if domain == _DOMAIN_CODING:
        selected = coding_selected
        fallback_chain = coding_chain
        source = "coding_route"
        reasons.append(
            f"domain=coding (strategy={strategy}, coding_conf={coding_conf}, chat_conf={chat_conf})"
        )
        reasons.append(f"coding_category={coding_decision.category}")
        # Append chat route as cross-domain fallback.
        if chat_selected:
            fallback_chain = fallback_chain + [chat_selected] + chat_chain
    else:
        selected = chat_selected
        fallback_chain = chat_chain
        source = "chat_route"
        reasons.append(
            f"domain=chat (strategy={strategy}, coding_conf={coding_conf}, chat_conf={chat_conf})"
        )
        reasons.append(f"chat_category={chat_decision.category} bucket={bucket}")
        # Append coding route as cross-domain fallback.
        if coding_selected:
            fallback_chain = fallback_chain + [coding_selected] + coding_chain

    # Global last fallback as final safety net.
    if not selected:
        global_fallback = bsl_cfg.get("global_last_fallback")
        if global_fallback:
            selected, fallback_chain = _extract_route(global_fallback)
            source = "global_last_fallback"
            reasons.append(f"global_last_fallback={selected}")
        else:
            source = "unresolved"
            reasons.append("no global_last_fallback configured")
    else:
        global_fallback = bsl_cfg.get("global_last_fallback")
        if global_fallback:
            glf_primary, glf_chain = _extract_route(global_fallback)
            if glf_primary:
                fallback_chain = fallback_chain + [glf_primary] + glf_chain

    return BSLAgenticMaxDecision(
        selected_model=selected,
        domain=domain,
        coding_category=coding_decision.category,
        chat_category=chat_decision.category,
        coding_confidence=coding_conf,
        chat_confidence=chat_conf,
        merge_strategy=strategy,
        depth=AGENTIC_MAX_DEPTH,
        source=source,
        reasons=reasons,
        fail_open=False,
        fallback_chain=fallback_chain,
    )


def _fail_open(reason: str = "exception") -> BSLAgenticMaxDecision:
    """Return a fail-open decision with no selected model."""
    return BSLAgenticMaxDecision(
        selected_model="",
        depth=AGENTIC_MAX_DEPTH,
        source="fail_open",
        reasons=[reason],
        fail_open=True,
    )


# ─── Main API ─────────────────────────────────────────────────────────────────


def route_bsl_agentic_max(request: ChatCompletionRequest, config: dict) -> BSLAgenticMaxDecision:
    """Route a ``model=blacksand-agentic-max`` request through dual-domain fusion.

    Runs BOTH the coding and chat classifiers, picks a winning domain under
    the configured merge strategy, and routes through that domain's matrix.
    The losing domain's route is appended as a cross-domain fallback.

    **Vision pre-flight:** If the request contains images, vision-capable
    models are prepended to the chain — coder-1/2/3 are text-only. If every
    vision route fails recoverably, the dispatcher advances to the dual-domain
    route, which self-answers. Uses the same shared ``select_vision_route``
    helper as the four sibling routers.

    ALWAYS ON (user directive 2026-08-06): routing no longer honors the
    ``enabled`` / ``tools.bsl_agentic_max_router`` flags. Catalog visibility is
    controlled separately via /v1/models.
    """
    try:
        bsl_cfg = _get_bsl_agentic_max_cfg(config)

        coding_decision = classify_coding_request_category(request)
        chat_decision = classify_request_category(request)
        complexity_decision = estimate_request_complexity(
            request, category_scores=chat_decision.scores
        )

        # ── Vision pre-flight: if request contains images, vision models go ──
        # first (prepended to the dual-domain chain). Live-registry selection,
        # identical to the four sibling routers.
        if _request_has_vision(request):
            try:
                from app.middleware.bsl_router_utils import select_vision_route

                vision_primary, vision_fallbacks = select_vision_route(config)
                if vision_primary:
                    base = _select_model(
                        config, coding_decision, chat_decision, complexity_decision.level
                    )
                    fusion_chain = []
                    if base.selected_model:
                        fusion_chain = [base.selected_model] + base.fallback_chain
                    glf = bsl_cfg.get("global_last_fallback")
                    if glf:
                        glf_primary, glf_chain = _extract_route(glf)
                        if glf_primary:
                            fusion_chain = fusion_chain + [glf_primary] + glf_chain
                    _seen = set()
                    fb_chain = [
                        e for e in (vision_fallbacks + fusion_chain)
                        if e and not (e in _seen or _seen.add(e))
                    ]
                    return BSLAgenticMaxDecision(
                        selected_model=vision_primary,
                        domain=base.domain,
                        coding_category=coding_decision.category,
                        chat_category=chat_decision.category,
                        coding_confidence=coding_decision.confidence,
                        chat_confidence=chat_decision.confidence,
                        merge_strategy=bsl_cfg.get("merge_strategy", MERGE_CONFIDENCE_WEIGHTED),
                        depth=AGENTIC_MAX_DEPTH,
                        source="vision_preflight",
                        reasons=[
                            "vision_preflight: request contains images",
                            f"vision chain: {vision_primary} → {fb_chain}",
                        ],
                        fail_open=False,
                        fallback_chain=fb_chain,
                    )
                # No vision routes available — fall through to dual-domain matrix.
            except Exception as _e:
                print(f"[BSLAgenticMax] vision pre-flight failed: {_e}", flush=True)

        return _select_model(config, coding_decision, chat_decision, complexity_decision.level)
    except Exception as exc:
        return _fail_open(reason=f"exception: {exc}")
