"""
xAI Grok contract.

Grok 4.x is OpenAI-compatible and tunes depth via a top-level
`reasoning_effort`. Reasoning is mandatory, so there is no disable path.
The explicit *-non-reasoning SKU is excluded so effort is never injected
into a model with no reasoning engine.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/grok.py"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    return prov.apply(
        payload, contract, "reasoning_effort", {"reasoning_effort": ctx.effort}
    )


CONTRACTS = [
    Contract(
        id="grok",
        source=SOURCE,
        priority=65,
        pattern=r"grok|xai",
        exclude=r"non-reasoning",
        apply=_apply,
    ),
]
