"""
OpenRouter contract.

OpenRouter is a transport/aggregator rather than a model vendor, but it
normalizes reasoning into its own `reasoning: {effort, exclude}` object
regardless of the underlying model, so it resolves as its own contract
and outranks the vendor families below it.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import coerce_effort

SOURCE = "families/openrouter.py"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    existing = payload.get("reasoning", {})
    if not isinstance(existing, dict):
        existing = {}
    existing["effort"] = coerce_effort(ctx.effort)
    existing["exclude"] = bool(existing.get("exclude", False))
    return prov.apply(payload, contract, "reasoning_object", {"reasoning": existing})


CONTRACTS = [
    Contract(
        id="openrouter",
        source=SOURCE,
        priority=95,
        pattern=r"openrouter|open-router",
        apply=_apply,
    ),
]
