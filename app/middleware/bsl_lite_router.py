"""
Middleware.bsl_lite_router — BSL-Lite Non-Agentic Single-Task Router (Phase P2)

BSL-Lite is the non-agentic single-task router. Unlike BSL-Chat (agentic
multi-step orchestration with structural prefilling and mock SSE), BSL-Lite
does pure single-task routing: one request → one response, no multi-step
orchestration.

"Lite" means non-agentic (single-task, no multi-step orchestration), NOT
single-model. BSL-Lite has an 8-agent matrix (mirroring OAC's primary agents:
Scout, Planner, Auditor, FastCoder, PowerCoder, UltraCoder, Refactor,
FrontendCoder), with its own independent model selection per agent.

BSL-Lite rule: one role at a time. The agent can run multiple calls
continuously, but within its role boundary only. If the pipeline needs
followup, it can suggest the next step, but never execute immediately —
just wait for user call/approval.

NO complexity estimation, NO buckets (fast/standard/deep). Those are
BSL-Chat concepts. BSL-Lite is pure task-route: classify → agent → model.

Design contract (master plan §1):
  - Domain: coding-agent single-task routing (Claude Code, Cursor, Aider)
  - Effort tier: L1 — single-task, non-agentic
  - Purpose: fast/cheap single-task routing, high volume, predictable cost
  - Difference from BSL-Chat: no structural prefilling, no mock SSE, no
    multi-step orchestration, no complexity buckets. Pure 1-request routing.

ALWAYS ON (user directive 2026-08-06): routing no longer honors the
``enabled``/``tools.bsl_lite_router`` flags. Catalog visibility is
controlled separately via /v1/models.

Config schema (canonical):
    bsl_models:
      bsl_lite:
        enabled: true
        category_overrides:
          scout:     { primary: "coder-1", fallback_1: "", fallback_2: "" }
          planner:   { primary: "coder-2", fallback_1: "", fallback_2: "" }
          ...
        global_last_fallback: "glm-5.2"

Both ``global_last_fallback`` and each agent slot accept either a bare string
(legacy) or a 3-slot dict (v2). This mirrors bsl-chat's _extract_route()
contract exactly.
"""

from dataclasses import dataclass, field
from typing import List

from app.middleware.coding_category_classifier import (
    classify_coding_request_category,
    CodingCategoryDecision,
    CATEGORY_GENERAL,
)
from app.models import ChatCompletionRequest

from app.middleware.bsl_router_utils import _extract_route, _request_has_vision


# ─── Constants ──────────────────────────────────────────────────────────────


def _get_bsl_lite_cfg(config: dict) -> dict:
    """Read bsl_lite config with canonical-then-legacy compatibility.

    Canonical path (future): ``bsl_models.bsl_lite``
    Legacy path: ``bsl_lite``

    Returns ``{}`` on None/non-dict to keep callers safe.
    """
    if not isinstance(config, dict):
        return {}
    bsl_models = config.get("bsl_models")
    if isinstance(bsl_models, dict):
        canonical = bsl_models.get("bsl_lite")
        if isinstance(canonical, dict):
            return canonical
    legacy = config.get("bsl_lite")
    if isinstance(legacy, dict):
        return legacy
    return {}


# ─── Decision dataclass ─────────────────────────────────────────────────────

@dataclass
class BSLLiteDecision:
    """Result of bsl-lite route selection."""
    selected_model: str = ""
    category: str = CATEGORY_GENERAL
    category_confidence: float = 0.0
    source: str = "disabled_default"
    reasons: List[str] = field(default_factory=list)
    fail_open: bool = False
    fallback_chain: List[str] = field(default_factory=list)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _select_model(
    config: dict,
    category_decision: CodingCategoryDecision,
) -> BSLLiteDecision:
    """Pure lookup logic. Assumes valid config structure.

    BSL-Lite is pure task-route: classify → agent → model. No complexity
    buckets. Each agent has one route (string or 3-slot dict).

    Precedence (no hardcoded safety net, empty = 503):
      0. default_route (bypasses entire matrix)
      1. category_override[classified_category]
      2. category_override[CATEGORY_GENERAL]  (scout fallback)
      3. global_last_fallback (always attempted when configured)
    """
    bsl_cfg = _get_bsl_lite_cfg(config)
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

    category_overrides = bsl_cfg.get("category_overrides", {}) or {}

    # 1. Category override for the classified agent (skip fallback category).
    if not selected and category != CATEGORY_GENERAL:
        agent_route = category_overrides.get(category)
        if agent_route:
            selected, fallback_chain = _extract_route(agent_route)
            source = "category_override"
            chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
            reasons.append(f"category_override[{category}]={chain_str}")
            # Append scout fallback to the chain.
            general_route = category_overrides.get(CATEGORY_GENERAL)
            if general_route:
                general_primary, general_fallbacks = _extract_route(general_route)
                if general_primary:
                    fallback_chain = fallback_chain + [general_primary] + general_fallbacks

    if not selected:
        # 2. Scout (general) fallback.
        general_route = category_overrides.get(CATEGORY_GENERAL)
        if general_route:
            selected, fallback_chain = _extract_route(general_route)
            source = "general_fallback"
            chain_str = " → ".join([selected] + fallback_chain) if fallback_chain else selected
            reasons.append(f"general_fallback={chain_str}")
            # Append global_last_fallback as final safety net in the chain.
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

    return BSLLiteDecision(
        selected_model=selected,
        category=category,
        category_confidence=category_decision.confidence,
        source=source,
        reasons=reasons,
        fail_open=False,
        fallback_chain=fallback_chain,
    )


def _fail_open(
    category: str = CATEGORY_GENERAL,
    reason: str = "exception",
) -> BSLLiteDecision:
    """Return a fail-open decision with no selected model."""
    return BSLLiteDecision(
        selected_model="",
        category=category,
        category_confidence=0.0,
        source="fail_open",
        reasons=[reason],
        fail_open=True,
    )


# ─── Main API ─────────────────────────────────────────────────────────────────

def route_bsl_lite(request: ChatCompletionRequest, config: dict) -> BSLLiteDecision:
    """Route a ``model=bsl-lite`` request through the agent matrix.

    BSL-Lite is the non-agentic single-task router: it classifies the request
    and selects a model from its 8-agent matrix. No complexity estimation,
    no buckets — pure task-route (classify → agent → model).

    The returned ``selected_model`` is an alias string (e.g. ``coder-2``) that
    should be fed into the existing combo/alias/provider resolver in ``main.py``.

    ALWAYS ON (user directive 2026-08-06): routing no longer honors the
    ``enabled``/``tools.bsl_lite_router`` flags. Catalog visibility is
    controlled separately via /v1/models.

    Vision pre-flight: image requests route through vision-capable models
    first; on recoverable vision failure the agent route self-answers.
    """
    try:
        bsl_cfg = _get_bsl_lite_cfg(config)

        # Classify regardless so logs/tests are stable.
        category_decision = classify_coding_request_category(request)

        # ALWAYS ON (user directive 2026-08-06): no enabled/tools gate.

        # ── Vision pre-flight: if request contains images, vision models go ──
        # first (prepended to the agent chain). Live-registry selection.
        if _request_has_vision(request):
            try:
                from app.middleware.bsl_router_utils import select_vision_route

                vision_primary, vision_fallbacks = select_vision_route(config)
                if vision_primary:
                    vision_decision = _select_model(config, category_decision)
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
                    return BSLLiteDecision(
                        selected_model=vision_primary,
                        category=category_decision.category,
                        category_confidence=category_decision.confidence,
                        source="vision_preflight",
                        reasons=[
                            "vision_preflight: request contains images",
                            f"vision chain: {vision_primary} → {fb_chain}",
                        ],
                        fail_open=False,
                        fallback_chain=fb_chain,
                    )
            except Exception as _e:
                print(f"[blacksand-lite] vision pre-flight failed: {_e}", flush=True)

        return _select_model(config, category_decision)
    except Exception as exc:
        return _fail_open(reason=f"exception: {exc}")
