"""
Shared utilities for BSL router modules.

Provides route-extraction helpers used by all BSL router implementations
(bsl_chat, bsl_lite, bsl_agentic, bsl_agentic_ultra, bsl_agentic_max).
Previously copy-pasted into each router file — now extracted for single-source maintenance.
"""

from __future__ import annotations
from typing import Any

# Slot order for the 3-slot cell schema (primary -> fallback_1 -> fallback_2).
_SLOT_ORDER: list[str] = ["primary", "fallback_1", "fallback_2"]

# Composite agent families whose granular sub-keys may fall back to the bare
# parent key. fast_coder/power_coder/ultra_coder/frontend_coder also contain
# "_" but their prefixes (fast, power, ...) are NOT real categories — they must
# never resolve to a parent route.
_COMPOSITE_AGENT_PARENTS = ("planner", "auditor")


def resolve_agent_route(agent_routes: dict, agent_key: str):
    """Resolve a possibly-granular agent key with parent fallback.

    'planner_architect' → agent_routes['planner_architect']
                        → agent_routes['planner']
                        → None
    """
    if not agent_key:
        return None
    route = agent_routes.get(agent_key)
    if route:
        return route
    if "_" in agent_key:
        parent = agent_key.split("_", 1)[0]
        # only treat as parent for known composite families
        if parent in _COMPOSITE_AGENT_PARENTS:
            return agent_routes.get(parent)
    return None


def _extract_route(cell_value) -> tuple[str, list[str]]:
    """Extract (primary_route, fallback_chain) from a config cell value.

    Handles two schemas:
    - Legacy: a bare string like "coder-2" -> ("coder-2", [])
    - v2 3-slot: a dict like {"primary": "coder-2", "fallback_1": "x", "fallback_2": "y"}
      -> ("coder-2", ["x", "y"])

    Returns ("", []) for None/empty/non-dict-non-str values.
    """
    if isinstance(cell_value, str) and cell_value:
        return (cell_value, [])
    if isinstance(cell_value, dict):
        chain = []
        for slot in _SLOT_ORDER:
            v = cell_value.get(slot)
            if isinstance(v, str) and v:
                chain.append(v)
        if chain:
            return (chain[0], chain[1:])
    return ("", [])


def _body_has_vision(body: dict) -> bool:
    """Check if a raw request body (dict) contains image content.

    Scans messages[].content for:
    - OpenAI format: {"type": "image_url", "image_url": {...}}
    - Anthropic format: {"type": "image", "source": {...}}
    - Gemini format: {"inlineData": {...}} or {"fileData": {...}}

    Returns True if any message contains at least one image part.
    """
    if not isinstance(body, dict):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type == "image_url":
                return True
            if part_type == "image" and part.get("source"):
                return True
            # Gemini-style inline data
            if part.get("inlineData") or part.get("fileData"):
                return True
    return False


def _request_has_vision(request) -> bool:
    """Check if a ChatCompletionRequest (Pydantic model) contains image content.

    Works with both the Pydantic model and raw dict bodies.
    Scans messages[].content for image_url or image content parts.
    """
    # If it's a raw dict, use _body_has_vision directly
    if isinstance(request, dict):
        return _body_has_vision(request)

    # Pydantic model path
    try:
        messages = getattr(request, "messages", None)
        if not messages:
            return False
        for msg in messages:
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for part in content:
                # Pydantic MessageContentPart model
                if hasattr(part, "type"):
                    if part.type == "image_url" and getattr(part, "image_url", None):
                        return True
                    if part.type == "image" and getattr(part, "source", None):
                        return True
                # Raw dict part (extra="allow" may preserve dict)
                elif isinstance(part, dict):
                    part_type = part.get("type", "")
                    if part_type == "image_url":
                        return True
                    if part_type == "image" and part.get("source"):
                        return True
                    if part.get("inlineData") or part.get("fileData"):
                        return True
    except Exception:
        pass
    return False


def select_vision_route(config: dict, max_slots: int = 3) -> tuple[str, list[str]]:
    """Select a vision-capable route chain from the live route registry.

    Vision pre-flight (user directive 2026-08-06): the returned chain is
    PREPENDED to the normal agent chain, so vision-capable models get the
    first attempt at reading the attached image. If every vision route fails
    with a recoverable error, the dispatcher advances to the normal agent
    route, which self-answers regardless of whether it can see the image.

    Selection rules:
      - Only enabled routes with ``has_vision`` are eligible.
      - One route per canonical family (no architectural redundancy).
      - Within a family, prefer reasoning-capable, then tool-capable, then
        deterministic alphabetical order.

    Returns ``("", [])`` when no vision-capable route is available (the
    caller then falls through to the normal chain — fail-open).
    """
    try:
        from app.middleware.route_registry import build_route_registry

        registry = build_route_registry(config, visible_only=True)
    except Exception as exc:
        print(f"[BSLVision] registry build failed (fail-open): {exc}", flush=True)
        return ("", [])

    try:
        family_best: dict[str, Any] = {}
        for fam, group in registry.items():
            candidates = [r for r in group.routes if r.enabled and r.has_vision]
            if not candidates:
                continue
            candidates.sort(
                key=lambda r: (
                    0 if r.has_reasoning else 1,
                    0 if r.has_tools else 1,
                    r.route_id,
                )
            )
            family_best[fam] = candidates[0]

        if not family_best:
            return ("", [])

        ordered = sorted(
            family_best.values(),
            key=lambda r: (
                0 if r.has_reasoning else 1,
                0 if r.has_tools else 1,
                r.route_id,
            ),
        )
        picked = [r.route_id for r in ordered[:max_slots]]
        return (picked[0], picked[1:])
    except Exception as exc:
        print(f"[BSLVision] selection failed (fail-open): {exc}", flush=True)
        return ("", [])

