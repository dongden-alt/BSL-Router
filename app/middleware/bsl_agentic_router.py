"""
Middleware.bsl_agentic_router — BSL-Agentic Fast Coding Orchestration Router (L2)

BSL-Agentic is the fast-tier agentic coding router. It targets coding agents
(Claude Code, Cursor, Aider, Openclaw) and adds agent-aware routing on top of
the flat bsl-lite matrix:

  - Per-agent 3-slot fallback chains (primary / fallback_1 / fallback_2)
  - Streaming-aware routing hints (interactive coding prefers streaming models)
  - NO complexity buckets, NO phase expansion, NO member spawning

Depth = fast. This is LOCKED. BSL-Agentic does single-agent routing:
  classify -> agent -> model chain. The "deep" tier (structural phase
  expansion + member spawning + multi-round quality gate) is exclusively
  Blacksand Code's domain and is NOT implemented here.

Safe-default OFF is REMOVED (user directive 2026-08-06): routing is
always-on, mirroring bsl-chat. The admin toggle controls ONLY catalog
visibility (/v1/models), never routing behavior.

Config schema (canonical):
    bsl_models:
      bsl_agentic:
        enabled: true
        agent_routes:
          scout:          { primary: "coder-1", fallback_1: "", fallback_2: "" }
          planner:        { primary: "coder-2", fallback_1: "", fallback_2: "" }
          auditor:        { primary: "coder-3", fallback_1: "", fallback_2: "" }
          fast_coder:     { primary: "coder-1", fallback_1: "", fallback_2: "" }
          power_coder:    { primary: "coder-2", fallback_1: "", fallback_2: "" }
          ultra_coder:    { primary: "coder-3", fallback_1: "", fallback_2: "" }
          refactor:       { primary: "coder-2", fallback_1: "", fallback_2: "" }
          frontend_coder: { primary: "coder-2", fallback_1: "", fallback_2: "" }
        global_last_fallback: "glm-5.2"

Both ``global_last_fallback`` and each agent slot accept either a bare string
(legacy) or a 3-slot dict (v2). Mirrors bsl-lite's _extract_route() contract.
"""

from dataclasses import dataclass, field
from typing import List

from app.middleware.coding_category_classifier import (
    classify_coding_request_category,
    CodingCategoryDecision,
    CATEGORY_GENERAL,
)
from app.models import ChatCompletionRequest

from app.middleware.bsl_router_utils import _extract_route, _request_has_vision, resolve_agent_route


# ─── Constants ──────────────────────────────────────────────────────────────

# Depth tier is LOCKED to fast. Not user-configurable. Deep is reserved for
# Blacksand Code's structural phase expansion (see orchestrator-loop.ts).
AGENTIC_DEPTH = "fast"


def _get_bsl_agentic_cfg(config: dict) -> dict:
    """Read bsl_agentic config with canonical-then-legacy compatibility.

    Canonical path: ``bsl_models.bsl_agentic``
    Legacy path: ``bsl_agentic``

    Returns ``{}`` on None/non-dict to keep callers safe.
    """
    if not isinstance(config, dict):
        return {}
    bsl_models = config.get("bsl_models")
    if isinstance(bsl_models, dict):
        canonical = bsl_models.get("bsl_agentic")
        if isinstance(canonical, dict):
            return canonical
    legacy = config.get("bsl_agentic")
    if isinstance(legacy, dict):
        return legacy
    return {}


# ─── Decision dataclass ─────────────────────────────────────────────────────
@dataclass
class BSLAgenticDecision:
    """Result of bsl-agentic route selection."""
    selected_model: str = ""
    category: str = CATEGORY_GENERAL
    category_confidence: float = 0.0
    depth: str = AGENTIC_DEPTH
    source: str = "disabled_default"
    reasons: List[str] = field(default_factory=list)
    fail_open: bool = False
    fallback_chain: List[str] = field(default_factory=list)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _select_model(
    config: dict,
    category_decision: CodingCategoryDecision,
) -> BSLAgenticDecision:
    """Pure lookup logic. Assumes valid config structure.

    BSL-Agentic is single-agent routing: classify -> agent -> model chain.
    Each agent has a 3-slot fallback chain (unlike bsl-lite's flat route).

    Precedence (no hardcoded safety net, empty = 503):
      0. default_route (bypasses entire matrix)
      1. agent_routes[classified_category]
      2. agent_routes[CATEGORY_GENERAL]  (scout fallback)
      3. global_last_fallback (always attempted when configured)
    """
    bsl_cfg = _get_bsl_agentic_cfg(config)
    category = category_decision.category
    reasons: List[str] = []

    selected = ""
    fallback_chain: List[str] = []
    source = "unresolved"

    # 0. Default route — all-in-one bypass.
    if bsl_cfg.get("default_route_enabled", False):
        default_route = bsl_cfg.get("default_route")
        if default_route:
            selected, fallback_chain = _extract_route(default_route)
            if selected:
                source = "default_route"
                chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
                reasons.append(f"default_route_override={chain_str}")
                reasons.append(f"category={category} (bypassed)")

    agent_routes = bsl_cfg.get("agent_routes", {}) or {}

    # 1. Agent route for the classified agent (skip fallback category).
    # Granular sub-agent keys (planner_architect, auditor_reviewer, ...)
    # resolve to their own cell first, then fall back to the bare parent
    # key (planner/auditor) — the bare key is the load-bearing parent
    # default. SCOUT-FIRST: the classification itself is the Scout step;
    # a non-general category means Scout routed the request to this agent.
    if not selected and category != CATEGORY_GENERAL:
        agent_route = resolve_agent_route(agent_routes, category)
        if agent_route:
            selected, fallback_chain = _extract_route(agent_route)
            source = "agent_route"
            chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
            reasons.append(f"agent_routes[{category}]={chain_str}")
            # Append scout fallback to the chain.
            general_route = agent_routes.get(CATEGORY_GENERAL)
            if general_route:
                general_primary, general_fallbacks = _extract_route(general_route)
                if general_primary:
                    fallback_chain = fallback_chain + [general_primary] + general_fallbacks

    if not selected:
        # 2. Scout (general) fallback — SCOUT-FIRST: trivial/general requests
        # are answered by Scout directly, no further agent routing.
        general_route = agent_routes.get(CATEGORY_GENERAL)
        if general_route:
            selected, fallback_chain = _extract_route(general_route)
            source = "scout_direct"
            chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
            reasons.append(f"scout_direct={chain_str}")
            # Append global_last_fallback as final safety net.
            global_fallback = bsl_cfg.get("global_last_fallback")
            if global_fallback:
                glf_primary, glf_chain = _extract_route(global_fallback)
                if glf_primary:
                    fallback_chain = fallback_chain + [glf_primary] + glf_chain
        else:
            # 3. Global last fallback — always attempted when configured.
            global_fallback = bsl_cfg.get("global_last_fallback")
            if global_fallback:
                selected, fallback_chain = _extract_route(global_fallback)
                source = "global_last_fallback"
                reasons.append(f"global_last_fallback={selected}")
            else:
                source = "unresolved"
                reasons.append("no global_last_fallback configured")

    return BSLAgenticDecision(
        selected_model=selected,
        category=category,
        category_confidence=category_decision.confidence,
        depth=AGENTIC_DEPTH,
        source=source,
        reasons=reasons,
        fail_open=False,
        fallback_chain=fallback_chain,
    )


def _fail_open(
    category: str = CATEGORY_GENERAL,
    reason: str = "exception",
) -> BSLAgenticDecision:
    """Return a fail-open decision with no selected model."""
    return BSLAgenticDecision(
        selected_model="",
        category=category,
        category_confidence=0.0,
        depth=AGENTIC_DEPTH,
        source="fail_open",
        reasons=[reason],
        fail_open=True,
    )


# ─── Main API ─────────────────────────────────────────────────────────────────


def route_bsl_agentic(request: ChatCompletionRequest, config: dict) -> BSLAgenticDecision:
    """Route a ``model=blacksand-agentic`` request through the agent matrix.

    BSL-Agentic is the fast-tier agentic coding router: classify the request,
    select an agent lane, and build a 3-slot fallback chain for that lane.
    No complexity buckets, no phase expansion, no member spawning.

    **Vision bypass:** If the request contains images, the coding matrix is
    bypassed entirely — coder-1/2/3 are text-only. Auto-select with
    ``require_vision=True`` picks the best vision-capable routes instead.
    Falls back to the coding matrix if no vision routes are available.

    The returned ``selected_model`` is an alias string (e.g. ``coder-2``) that
    should be fed into the existing combo/alias/provider resolver in ``main.py``.

    ALWAYS ON (user directive 2026-08-06): routing no longer honors the
    ``enabled``/``tools.bsl_agentic_router`` flags. Catalog visibility is
    controlled separately via /v1/models.
    """
    try:
        bsl_cfg = _get_bsl_agentic_cfg(config)

        # Classify regardless so logs/tests are stable (Scout-first step).
        category_decision = classify_coding_request_category(request)

        # ── Vision pre-flight: if request contains images, vision models go ──
        # first (prepended to the agent chain). coder-1/2/3 are text-only.
        # If all vision routes fail recoverably, the dispatcher advances to the
        # agent route, which self-answers. Live-registry selection replaces the
        # old bsl_lite-targeted auto-select (which had 0 routes and never fired).
        if _request_has_vision(request):
            try:
                from app.middleware.bsl_router_utils import select_vision_route

                vision_primary, vision_fallbacks = select_vision_route(config)
                if vision_primary:
                    vision_decision = _select_model(config, category_decision)
                    # Append the normal agent chain AFTER the vision chain so
                    # the next agent handles it when vision fails.
                    agent_chain = []
                    if vision_decision.selected_model:
                        agent_chain = [vision_decision.selected_model] + vision_decision.fallback_chain
                    glf = bsl_cfg.get("global_last_fallback")
                    if glf:
                        glf_primary, glf_chain = _extract_route(glf)
                        if glf_primary:
                            agent_chain = agent_chain + [glf_primary] + glf_chain
                    _seen = set()
                    fb_chain = [e for e in (vision_fallbacks + agent_chain)
                                if e and not (e in _seen or _seen.add(e))]
                    return BSLAgenticDecision(
                        selected_model=vision_primary,
                        category=category_decision.category,
                        category_confidence=category_decision.confidence,
                        depth=AGENTIC_DEPTH,
                        source="vision_preflight",
                        reasons=[
                            "vision_preflight: request contains images",
                            f"vision chain: {vision_primary} → {fb_chain}",
                        ],
                        fail_open=False,
                        fallback_chain=fb_chain,
                    )
                else:
                    # No vision routes available — fall through to coding matrix
                    # (next agent self-answers).
                    pass
            except Exception as _e:
                print(f"[BSLAgentic] vision pre-flight failed: {_e}", flush=True)

        return _select_model(config, category_decision)
    except Exception as exc:
        return _fail_open(reason=f"exception: {exc}")
