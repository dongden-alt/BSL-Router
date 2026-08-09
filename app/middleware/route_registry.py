"""
Middleware.route_registry — canonical family ↔ concrete route mapper.

Pure, stateless utility. Scans the user's config to build a map of all
available routes, normalizes each model ID to its canonical family (stripping
thinking/effort suffixes), and provides lookup APIs the bsl_chat matrix
dispatcher uses to resolve canonical-family fallback chains to concrete routes.

This module does NOT modify the existing resolution pipeline in main.py.
It is a read-only lookup layer that sits alongside the resolver.

Design rules (from KI §4.1 + §9.1):
- Route candidate metadata: route_id, resolver_id, canonical_id, provider_id,
  protocol, capabilities, status, cost tier.
- Matrix stores canonical families; runtime resolves via this registry.
- Pure functions: no IO, no LLM, no side effects.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ─── Canonical normalization ────────────────────────────────────────────────

# Suffixes to strip for canonical family grouping.
# Order matters: longer suffixes first to avoid partial strips.
_THINKING_SUFFIXES = [
    # Effort/level suffixes
    "-max", "-xhigh", "-ultra", "-high", "-medium", "-low",
    # Reasoning mode suffixes
    "-non-reasoning", "-reasoning", "-thinking", "-minimal",
    # Special variants (NOT -pro/-nano — those are part of model names)
    "-antigravity", "-antigravity-ultra", "-request", "-request-antigravity",
    "-free", "-oss",
    # Date/version suffixes (e.g., -preview)
    "-preview",
]

# Compiled regex for date-like suffixes: -2025-01, -2026-0315, etc.
# Only matches year-based suffixes (starting with 20), not version numbers like -0309.
_DATE_SUFFIX_RE = re.compile(r"-20\d{2}[-/]?\d{0,4}$", re.IGNORECASE)

# Common model ID normalization: dashes/underscores/dots are equivalent.
_SEPARATORS_RE = re.compile(r"[-_.]")


def normalize_canonical(model_id: str) -> str:
    """Normalize a raw model ID to its canonical family name.

    Strips thinking/effort/reasoning suffixes and normalizes separators.
    Examples:
        "gpt-5.6-sol-max"         → "gpt-5-6-sol"
        "claude-opus-4.8-thinking" → "claude-opus-4-8"
        "glm-5.2-anthropic"        → "glm-5-2"
        "deepseek-v4-pro"          → "deepseek-v-4-pro"
        "DeepSeek-V4-Flash"        → "deepseek-v-4-flash"
        "MiniMax-M3"               → "minimax-m3"
        "kimi-k2.7-code"           → "kimi-k2-7-code"

    The result is lowercase with dashes only (no dots/underscores).
    """
    if not model_id:
        return ""
    result = model_id.strip().lower()
    # Strip known suffixes (longest first)
    for suffix in sorted(_THINKING_SUFFIXES, key=len, reverse=True):
        if result.endswith(suffix):
            result = result[: -len(suffix)]
            break
    # Strip date-like suffixes
    result = _DATE_SUFFIX_RE.sub("", result)
    # Normalize separators: dots and underscores → dashes
    result = result.replace(".", "-").replace("_", "-")
    # Collapse multiple dashes
    result = re.sub(r"-+", "-", result)
    return result.strip("-")


# ─── Data classes ───────────────────────────────────────────────────────────


@dataclass
class RouteCandidate:
    """A concrete route in the user's pool (KI §4.1)."""

    route_id: str  # "provider/model" format, unique identifier
    resolver_id: str  # raw model ID for the resolver (e.g. "gpt-5.6-sol")
    canonical_id: str  # normalized canonical family (e.g. "gpt-5-6-sol")
    provider_id: str  # provider key in config
    model_id: str  # exact model ID in provider's model list
    enabled: bool = True
    protocol: str = ""  # "openai-compatible", "anthropic", "gemini"
    has_vision: bool = False
    has_tools: bool = False
    has_reasoning: bool = False

    def __repr__(self) -> str:
        return f"RouteCandidate({self.route_id} → {self.canonical_id})"


@dataclass
class FamilyGroup:
    """All concrete routes sharing one canonical family."""

    canonical_id: str
    routes: List[RouteCandidate] = field(default_factory=list)

    @property
    def is_available(self) -> bool:
        """True if at least one enabled route exists."""
        return any(r.enabled for r in self.routes)

    @property
    def enabled_routes(self) -> List[RouteCandidate]:
        return [r for r in self.routes if r.enabled]


# ─── Registry builder ───────────────────────────────────────────────────────


def _infer_protocol(provider_data: dict) -> str:
    """Best-effort protocol detection from provider config."""
    if not isinstance(provider_data, dict):
        return ""
    base_url = (provider_data.get("base_url") or "").lower()
    if "anthropic" in base_url:
        return "anthropic"
    if "gemini" in base_url or "googleapis" in base_url:
        return "gemini"
    return "openai-compatible"


def _detect_capabilities(model_entry: dict, model_id_lower: str) -> Tuple[bool, bool, bool]:
    """Detect vision/tools/reasoning from model entry + ID heuristics."""
    vision = bool(model_entry.get("vision", False)) if isinstance(model_entry, dict) else False
    tools = bool(model_entry.get("tools", True)) if isinstance(model_entry, dict) else True
    # Reasoning if not explicitly non-reasoning
    reasoning = "-non-reasoning" not in model_id_lower and "-minimal" not in model_id_lower
    # Vision heuristic: common vision-capable family prefixes
    if not vision:
        for prefix in ("claude-opus", "claude-sonnet", "gpt-5", "gemini", "grok", "mimo-v2-omni"):
            if model_id_lower.startswith(prefix):
                vision = True
                break
    return vision, tools, reasoning


def build_route_registry(
    config: dict, visible_only: bool = True
) -> Dict[str, FamilyGroup]:
    """Scan the user's config and build a canonical-family → routes map.

    Reads config["providers"] to enumerate all provider/model combinations.
    Returns a dict keyed by canonical_id, each value a FamilyGroup.

    Args:
        config: The full BSL config dict with a "providers" key.
        visible_only: If True (default), skip providers marked hidden:true.
            Hidden providers are excluded from auto-selection so they never
            leak into UI or matrix recommendations, while remaining available
            for direct dispatch.

    Pure function: no IO, no side effects, deterministic output.
    """
    registry: Dict[str, FamilyGroup] = {}
    if not isinstance(config, dict):
        return registry

    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        return registry

    for provider_id, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue
        # Visibility gate: skip hidden providers when visible_only is set.
        if visible_only and provider_data.get("hidden", False):
            continue
        # Skip the built-in Blacksand Labs / BSL provider. Its "models"
        # (bsl-chat, bsl-lite, ...) are virtual family entrypoints resolved by
        # the router's own dispatchers — they are NOT dispatchable leaf routes
        # and must never appear as auto-select candidates (would self-refer).
        if provider_id == "blacksand" or provider_data.get("type") == "bsl" or provider_data.get("format") == "bsl":
            continue
        protocol = _infer_protocol(provider_data)
        models = provider_data.get("models", [])
        if not isinstance(models, list):
            continue

        for model_entry in models:
            if not isinstance(model_entry, dict):
                continue
            model_id = model_entry.get("id", "")
            if not model_id:
                continue

            enabled = model_entry.get("enabled", True)
            model_id_lower = model_id.lower()
            vision, tools, reasoning = _detect_capabilities(model_entry, model_id_lower)
            canonical = normalize_canonical(model_id)
            route_id = f"{provider_id}/{model_id}"

            candidate = RouteCandidate(
                route_id=route_id,
                resolver_id=model_id,
                canonical_id=canonical,
                provider_id=provider_id,
                model_id=model_id,
                enabled=enabled,
                protocol=protocol,
                has_vision=vision,
                has_tools=tools,
                has_reasoning=reasoning,
            )

            if canonical not in registry:
                registry[canonical] = FamilyGroup(canonical_id=canonical)
            registry[canonical].routes.append(candidate)

    return registry


# ─── Lookup API ─────────────────────────────────────────────────────────────


def find_routes_for_family(
    registry: Dict[str, FamilyGroup],
    canonical_family: str,
    enabled_only: bool = True,
) -> List[RouteCandidate]:
    """Find all concrete routes matching a canonical family.

    The canonical_family is normalized before lookup so callers can pass
    either "claude-opus-4.8" or "claude-opus-4-8" — both resolve the same.
    """
    normalized = normalize_canonical(canonical_family)
    family = registry.get(normalized)
    if not family:
        return []
    if enabled_only:
        return sorted(family.enabled_routes, key=lambda r: r.route_id)
    return sorted(family.routes, key=lambda r: r.route_id)


def resolve_canonical_chain(
    registry: Dict[str, FamilyGroup],
    canonical_chain: List[str],
    enabled_only: bool = True,
) -> Optional[Tuple[str, str, str]]:
    """Resolve a canonical-family fallback chain to the first available route.

    Args:
        registry: The route registry from build_route_registry().
        canonical_chain: Ordered list like ["claude-opus-4.8", "gpt-5.6-sol"].
        enabled_only: If True, skip disabled routes.

    Returns:
        (canonical_id, provider_id, model_id) for the first available family,
        or None if no family in the chain has available routes.
    """
    for family_name in canonical_chain:
        routes = find_routes_for_family(registry, family_name, enabled_only)
        if routes:
            route = routes[0]  # First available route for this family
            return (route.canonical_id, route.provider_id, route.model_id)
    return None


def resolve_full_chain(
    registry: Dict[str, FamilyGroup],
    canonical_chain: List[str],
    enabled_only: bool = True,
) -> List[Tuple[str, str, str]]:
    """Resolve a canonical-family chain to ALL available concrete routes.

    Unlike resolve_canonical_chain (which returns the first match), this
    returns the full list of (canonical, provider, model) tuples across
    ALL families in the chain — used for building the concrete fallback list.

    Returns:
        List of (canonical_id, provider_id, model_id) tuples, one per
        available route, in chain order.
    """
    result: List[Tuple[str, str, str]] = []
    for family_name in canonical_chain:
        routes = find_routes_for_family(registry, family_name, enabled_only)
        for route in routes:
            result.append((route.canonical_id, route.provider_id, route.model_id))
    return result


def list_available_families(
    registry: Dict[str, FamilyGroup],
    enabled_only: bool = True,
) -> List[str]:
    """Return a sorted list of all canonical families in the pool."""
    families = [
        fam.canonical_id
        for fam in registry.values()
        if not enabled_only or fam.is_available
    ]
    return sorted(set(families))


def coverage_report(
    registry: Dict[str, FamilyGroup],
    required_families: List[str],
) -> Dict[str, bool]:
    """Check which canonical families are present in the user's pool.

    Args:
        registry: The route registry.
        required_families: List of canonical family names to check.

    Returns:
        Dict mapping each family → True if available, False if missing.
    """
    available = set(list_available_families(registry, enabled_only=True))
    return {
        normalize_canonical(f): normalize_canonical(f) in available
        for f in required_families
    }
