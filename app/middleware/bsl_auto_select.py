"""
Middleware.bsl_auto_select — Benchmark-ordered route selection engine.

Implements the auto-select algorithm (master plan §4.2 hardened v2):
  1. Build a route registry from config, filtering hidden providers.
  2. Load the benchmark triplet (P, F1, F2) for the target cell.
  3. Resolve the triplet in order: one unique canonical family per slot.
     - If a family is already used in a prior slot, skip it.
     - If a family is missing from the pool, warn and leave the slot empty.
  4. Backfill any remaining empty slots with the highest-scoring unused
     families in the registry (score 0 is accepted as last resort).
  5. Within each family, pick the best concrete route using health-gated
     tie-breaking: circuit-breaker state (if available), capability match,
     then deterministic alphabetical fallback.
  6. Append global_last_fallback as the terminal safety net.

Design rules:
  - LEAF ROUTES ONLY (no combos). §4.4
  - One canonical family per slot — no architectural redundancy. §4.2
  - Never auto-apply over a manual override. §4.2
  - Pure function: no IO, no side effects. Deterministic for same input.
  - Fail-open: if the circuit breaker is unavailable, skip health gate.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
import time

from app.middleware.route_registry import (
    RouteCandidate,
    FamilyGroup,
    build_route_registry,
    normalize_canonical,
)
from app.middleware.bsl_benchmark_sheet import (
    get_family_quality_score as _chat_get_quality,
    get_cell_families as _chat_get_families,
    GLOBAL_LAST_FALLBACK_FAMILY as _CHAT_GLF,
    ALL_CATEGORIES as _CHAT_CATS,
    ALL_TIERS as _CHAT_TIERS,
)
from app.middleware.bsl_lite_benchmark_sheet import (
    get_family_quality_score as _lite_get_quality,
    get_cell_families as _lite_get_families,
    GLOBAL_LAST_FALLBACK_FAMILY as _LITE_GLF,
    ALL_CATEGORIES as _LITE_CATS,
    ALL_TIERS as _LITE_TIERS,
)
from app.middleware.bsl_agentic_benchmark_sheet import (
    get_family_quality_score as _agentic_get_quality,
    get_cell_families as _agentic_get_families,
    GLOBAL_LAST_FALLBACK_FAMILY as _AGENTIC_GLF,
    ALL_CATEGORIES as _AGENTIC_CATS,
    ALL_TIERS as _AGENTIC_TIERS,
)


def _get_sheet(target: str = "bsl_chat"):
    """Return the (quality_fn, families_fn, glf, categories, tiers) tuple for the target."""
    if target == "bsl_lite":
        return (_lite_get_quality, _lite_get_families, _LITE_GLF, _LITE_CATS, _LITE_TIERS)
    if target == "bsl_agentic":
        return (_agentic_get_quality, _agentic_get_families, _AGENTIC_GLF, _AGENTIC_CATS, _AGENTIC_TIERS)
    return (_chat_get_quality, _chat_get_families, _CHAT_GLF, _CHAT_CATS, _CHAT_TIERS)

# ─── Data classes ───────────────────────────────────────────────────────────


@dataclass
class ScoredRoute:
    """A route with its computed score components."""
    route: RouteCandidate
    quality_score: float       # canonical family benchmark score (0-11)
    health_score: float        # route health proxy (0-3)
    diversity_score: float     # provider diversity bonus (computed in context)
    total_score: float         # weighted sum
    reason: str                # human-readable explanation


@dataclass
class AutoSelectResult:
    """The output of auto_select_cell — a complete cell recommendation."""
    primary: Optional[ScoredRoute]
    fallback_1: Optional[ScoredRoute]
    fallback_2: Optional[ScoredRoute]
    global_last_fallback: str
    category: str
    complexity_tier: str
    explanation: str           # full human-readable chain explanation
    warnings: List[str] = field(default_factory=list)


# ─── Hard filters (§4.2 step 3) ─────────────────────────────────────────────


def _passes_hard_filters(
    route: RouteCandidate,
    require_vision: bool = False,
    require_tools: bool = False,
    require_reasoning: bool = False,
) -> Tuple[bool, str]:
    """Check if a route passes all hard filters.

    Returns (passes, rejection_reason).
    """
    if not route.enabled:
        return False, "route disabled"
    if require_vision and not route.has_vision:
        return False, "vision required but route lacks it"
    if require_tools and not route.has_tools:
        return False, "tools required but route lacks them"
    if require_reasoning and not route.has_reasoning:
        return False, "reasoning required but route is non-reasoning"
    return True, ""


# ─── Health gate (§4.2 step 3b — optional circuit-breaker integration) ─────


# Type alias for the health-check callback. Returns (is_healthy, reason).
HealthCheckFn = Optional[Callable[[str, str], Tuple[bool, str]]]


def _default_health_check(provider_id: str, model_id: str) -> Tuple[bool, str]:
    """Default health check: always healthy (no live data in v1)."""
    return True, ""


def _make_health_check_from_breaker() -> HealthCheckFn:
    """Build a health-check callback from the global circuit breaker singleton.

    The circuit breaker tracks health at (provider, model, connection_index)
    granularity. For route-level selection we aggregate: a route is unhealthy
    only if ALL its connections are OPEN.

    Fail-open: if the breaker is unavailable or not enabled, returns None
    (no health gate applied).
    """
    try:
        from app.circuit_breaker import get_breaker
        breaker = get_breaker()
    except Exception:
        return None

    if not breaker or not breaker.enabled:
        return None

    def _check(provider_id: str, model_id: str) -> Tuple[bool, str]:
        """Check if any connection for this (provider, model) is not OPEN."""
        try:
            # Scan breaker state for all connection indices of this route.
            # The breaker keys are "provider/model/conn_index".
            prefix = f"{provider_id}/{model_id}/"
            all_open = True
            open_count = 0
            total = 0
            for key, state in breaker.state.items():
                if key.startswith(prefix):
                    total += 1
                    current = state.get("state", "CLOSED")
                    # Refresh OPEN → HALF_OPEN if recovery timeout expired
                    if current == "OPEN":
                        open_until = state.get("open_until", 0.0)
                        if time.time() >= open_until:
                            current = "HALF_OPEN"
                    if current == "OPEN":
                        open_count += 1
                    else:
                        all_open = False
                        break  # At least one healthy connection
            if total == 0:
                return True, ""  # No tracked connections → assume healthy
            if all_open:
                return False, f"all {open_count} connections OPEN"
            return True, ""
        except Exception:
            return True, ""  # Fail-open

    return _check


# ─── Within-family best route selection ────────────────────────────────────


def _best_route_for_family(
    routes: List[RouteCandidate],
    complexity_tier: str,
    health_check: HealthCheckFn = None,
    require_vision: bool = False,
) -> Optional[RouteCandidate]:
    """Pick the best concrete route within a canonical family.

    Tie-breaking order (deterministic):
      1. Health gate: skip routes where all connections are OPEN (if breaker available).
      2. Hard capability filter: skip routes lacking required capabilities (vision/tools/reasoning).
      3. Reasoning capability for deep tier (prefer reasoning-capable routes).
      4. Tool capability (prefer tool-capable routes).
      5. Alphabetical by route_id (deterministic fallback).

    Returns None if no route passes the health + capability gates.
    """
    if not routes:
        return None

    # Use the provided health check or default (always healthy)
    check = health_check or _default_health_check

    # Filter by health gate + hard capability filters
    healthy_routes: List[RouteCandidate] = []
    for route in routes:
        if not route.enabled:
            continue
        # Hard capability filters — applied DURING selection, not after
        if require_vision and not route.has_vision:
            continue
        is_healthy, _reason = check(route.provider_id, route.model_id)
        if is_healthy:
            healthy_routes.append(route)

    if not healthy_routes:
        # Fail-open: if all routes fail health/capability check, use all enabled routes
        # that pass capability filters (avoid deadlock, but still respect hard caps)
        healthy_routes = [r for r in routes if r.enabled and (not require_vision or r.has_vision)]
        if not healthy_routes:
            return None

    # Sort by capability preferences + deterministic tiebreak
    def _sort_key(route: RouteCandidate) -> Tuple[int, int, str]:
        # Reasoning preference: deep tier wants reasoning models first
        reasoning_rank = 0
        if complexity_tier == "deep":
            reasoning_rank = 0 if route.has_reasoning else 1
        # Tool preference: tool-capable routes rank higher
        tools_rank = 0 if route.has_tools else 1
        return (reasoning_rank, tools_rank, route.route_id)

    healthy_routes.sort(key=_sort_key)
    return healthy_routes[0]


# ─── Core resolution engine ─────────────────────────────────────────────────


def _resolve_families_to_slots(
    registry: Dict[str, FamilyGroup],
    category: str,
    complexity_tier: str,
    health_check: HealthCheckFn = None,
    target: str = "bsl_chat",
    require_vision: bool = False,
    require_tools: bool = False,
    require_reasoning: bool = False,
) -> Tuple[List[ScoredRoute], List[str]]:
    """Resolve benchmark triplet to concrete routes, one unique family per slot.

    Algorithm:
      1. Load the benchmark triplet: primary_fam, fb1_fam, fb2_fam.
      2. For each slot (P → F1 → F2) in order:
         a. If the family is already used in a previous slot, skip to the next
            family in the triplet (maintaining architectural diversity).
         b. Look up the family in the registry.
         c. Pick the best concrete route within the family (health-gated).
         d. If found, mark the canonical_id as used.
      3. If after consuming the triplet some slots are still empty, backfill
         from remaining families ordered by benchmark quality score (desc).
         Score-0 families are accepted as last resort to guarantee 100% coverage.
      4. Never assign the same canonical family to two slots in the same cell.

    Returns (picked_routes, warnings).
    """
    warnings: List[str] = []
    used_families: set = set()
    picked: List[ScoredRoute] = []

    _get_quality, _get_families, _glf, _cats, _tiers = _get_sheet(target)
    # Load the benchmark triplet (canonical family names)
    primary_fam, fb1_fam, fb2_fam = _get_families(category, complexity_tier)
    triplet = [primary_fam, fb1_fam, fb2_fam]
    slot_names = ["primary", "fallback_1", "fallback_2"]

    # Phase 1: Resolve benchmark triplet in order, skip used families
    for slot_idx, fam in enumerate(triplet):
        norm_fam = normalize_canonical(fam)
        if norm_fam in used_families:
            warnings.append(
                f"Slot {slot_names[slot_idx]}: family '{fam}' already used, skipping"
            )
            continue

        family_group = registry.get(norm_fam)
        if not family_group or not family_group.enabled_routes:
            warnings.append(
                f"Slot {slot_names[slot_idx]}: family '{fam}' has no enabled routes in pool"
            )
            continue

        best = _best_route_for_family(family_group.routes, complexity_tier, health_check, require_vision=require_vision)
        if not best:
            warnings.append(
                f"Slot {slot_names[slot_idx]}: family '{fam}' has no healthy routes"
            )
            continue

        quality = _get_quality(best.canonical_id, category, complexity_tier)
        total = quality  # No diversity bonus needed — each family is unique by design

        picked.append(ScoredRoute(
            route=best,
            quality_score=quality,
            health_score=1.0,
            diversity_score=0.0,
            total_score=total,
            reason=f"{best.route_id} ({best.canonical_id}): quality={quality:.1f}",
        ))
        used_families.add(norm_fam)

    # Phase 2: Backfill remaining slots with best unused families
    if len(picked) < 3:
        # Build a sorted list of families by benchmark quality, excluding used ones
        remaining_families: List[Tuple[str, float]] = []
        for canonical_key, family_group in registry.items():
            if canonical_key in used_families:
                continue
            if not family_group.enabled_routes:
                continue
            score = _get_quality(canonical_key, category, complexity_tier)
            remaining_families.append((canonical_key, score))
        # Sort by score DESC, then canonical_key ASC (deterministic for ties)
        remaining_families.sort(key=lambda x: (-x[1], x[0]))

        for can_key, _score in remaining_families:
            if len(picked) >= 3:
                break
            if can_key in used_families:
                continue
            fg = registry[can_key]
            best = _best_route_for_family(fg.routes, complexity_tier, health_check, require_vision=require_vision)
            if not best:
                continue
            quality = _get_quality(best.canonical_id, category, complexity_tier)
            slot_name = slot_names[len(picked)] if len(picked) < 3 else "overflow"
            picked.append(ScoredRoute(
                route=best,
                quality_score=quality,
                health_score=1.0,
                diversity_score=0.0,
                total_score=quality,
                reason=f"{best.route_id} ({best.canonical_id}): quality={quality:.1f} [backfill → {slot_name}]",
            ))
            used_families.add(can_key)

        if len(picked) < 3:
            warnings.append(
                f"Only {len(picked)}/3 slots filled — {3 - len(picked)} slots have no eligible routes"
            )

    return picked, warnings


# ─── Main API ───────────────────────────────────────────────────────────────


def auto_select_cell(
    config: dict,
    category: str,
    complexity_tier: str,
    require_vision: bool = False,
    require_tools: bool = False,
    require_reasoning: bool = False,
    health_check: HealthCheckFn = None,
    target: str = "bsl_chat",
) -> AutoSelectResult:
    """Auto-select the best route chain for a matrix cell.

    Implements the hardened v2 algorithm: benchmark-ordered triplet resolution
    with family uniqueness, deterministic backfill, and optional health gate.

    Args:
        config: The full BSL config dict.
        category: One of ALL_CATEGORIES (e.g. "general", "finance").
        complexity_tier: One of ALL_TIERS ("fast", "standard", "deep").
        require_vision: If True, only select routes with vision capability.
        require_tools: If True, only select routes with tool capability.
        require_reasoning: If True, only select reasoning-capable routes.
        health_check: Optional callback(provider_id, model_id) → (is_healthy, reason).
            If None, the circuit breaker is consulted automatically (fail-open).

    Returns:
        AutoSelectResult with primary, fallback_1, fallback_2, and explanation.
    """
    _get_quality, _get_families, _glf, _cats, _tiers = _get_sheet(target)
    warnings: List[str] = []

    # Step 1: Build registry from config (exclude hidden providers)
    if config is None or not isinstance(config, dict):
        return AutoSelectResult(
            primary=None, fallback_1=None, fallback_2=None,
            global_last_fallback=_glf,
            category=category, complexity_tier=complexity_tier,
            explanation="No config provided; using global_last_fallback only.",
            warnings=["Config is None or invalid"],
        )
    registry = build_route_registry(config, visible_only=True)

    # Step 2: Check if we have any routes at all
    total_routes = sum(len(fg.enabled_routes) for fg in registry.values())
    if total_routes == 0:
        primary_fam, fb1_fam, fb2_fam = _get_families(category, complexity_tier)
        warnings.append(
            f"No enabled routes in visible pool for {category}/{complexity_tier}. "
            f"Benchmark recommends: {primary_fam}, {fb1_fam}, {fb2_fam}"
        )
        return AutoSelectResult(
            primary=None, fallback_1=None, fallback_2=None,
            global_last_fallback=_glf,
            category=category, complexity_tier=complexity_tier,
            explanation="No routes available; using global_last_fallback only.",
            warnings=warnings,
        )

    # Step 3: Resolve health-check callback (circuit breaker if not provided)
    if health_check is None:
        health_check = _make_health_check_from_breaker()

    # Step 4: Resolve families to slots (benchmark-ordered, unique per cell)
    picked, resolve_warnings = _resolve_families_to_slots(
        registry, category, complexity_tier, health_check, target=target,
        require_vision=require_vision, require_tools=require_tools, require_reasoning=require_reasoning,
    )
    warnings.extend(resolve_warnings)

    # Step 5: Apply capability filters as soft constraints on the picked routes
    filtered_picked: List[ScoredRoute] = []
    for sr in picked:
        passes, reason = _passes_hard_filters(
            sr.route,
            require_vision=require_vision,
            require_tools=require_tools,
            require_reasoning=require_reasoning,
        )
        if passes:
            filtered_picked.append(sr)
        else:
            warnings.append(f"Route {sr.route.route_id} filtered: {reason}")

    # Step 6: Assign slots
    primary = filtered_picked[0] if len(filtered_picked) >= 1 else None
    fallback_1 = filtered_picked[1] if len(filtered_picked) >= 2 else None
    fallback_2 = filtered_picked[2] if len(filtered_picked) >= 3 else None

    # Step 7: Benchmark mismatch warning
    recommended_primary, _, _ = _get_families(category, complexity_tier)
    if primary and primary.route.canonical_id != normalize_canonical(recommended_primary):
        warnings.append(
            f"Benchmark recommends '{recommended_primary}' for {category}/{complexity_tier}, "
            f"but best available is '{primary.route.canonical_id}' "
            f"(score={primary.total_score:.1f})."
        )

    # Step 8: Build explanation
    chain_desc = " → ".join(s.reason for s in filtered_picked)
    explanation = (
        f"Auto-select for [{category}/{complexity_tier}]: {chain_desc}. "
        f"Global fallback: {_glf}."
    )

    return AutoSelectResult(
        primary=primary,
        fallback_1=fallback_1,
        fallback_2=fallback_2,
        global_last_fallback=_glf,
        category=category,
        complexity_tier=complexity_tier,
        explanation=explanation,
        warnings=warnings,
    )


def auto_select_full_matrix(config: dict, target: str = "bsl_chat") -> Dict[str, Dict[str, AutoSelectResult]]:
    """Run auto-select for all cells in the target matrix.

    Returns category → tier → AutoSelectResult.
    """
    _get_quality, _get_families, _glf, _cats, _tiers = _get_sheet(target)
    matrix: Dict[str, Dict[str, AutoSelectResult]] = {}
    for category in _cats:
        matrix[category] = {}
        for tier in _tiers:
            matrix[category][tier] = auto_select_cell(config, category, tier, target=target)
    return matrix


def result_to_chain(result: AutoSelectResult) -> List[str]:
    """Extract the selected route_id chain from an AutoSelectResult.

    The primary/fallback slots are concrete route_ids. The tail is
    GLOBAL_LAST_FALLBACK_FAMILY, which is a canonical family name, not a
    dispatchable route_id, pending P2.5 resolver wiring.
    """
    chain: List[str] = []
    for slot in (result.primary, result.fallback_1, result.fallback_2):
        if slot and slot.route:
            chain.append(slot.route.route_id)
    chain.append(result.global_last_fallback)
    # Dedupe preserving order
    seen: set = set()
    return [r for r in chain if r and not (r in seen or seen.add(r))]
