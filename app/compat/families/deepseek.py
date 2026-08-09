"""
DeepSeek family contract.

DeepSeek V4 accepts all three reasoning shapes simultaneously across the
reseller channels it is served on: the Anthropic-style `thinking` object,
a top-level `reasoning_effort`, and an `output_config.effort` level.
Sending all three maximizes the chance the routed channel honors one,
which is why this contract is deliberately redundant.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import coerce_effort

SOURCE = "families/deepseek.py"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    oc = payload.get("output_config", {})
    if not isinstance(oc, dict):
        oc = {}
    oc["effort"] = coerce_effort(ctx.effort)
    return prov.apply(
        payload,
        contract,
        "triple_shape",
        {
            "thinking": {"type": "enabled"},
            "reasoning_effort": ctx.effort,
            "output_config": oc,
        },
    )


CONTRACTS = [
    Contract(
        id="deepseek-v4",
        source=SOURCE,
        priority=60,
        pattern=r"deepseek-v4",
        apply=_apply,
    ),
]
