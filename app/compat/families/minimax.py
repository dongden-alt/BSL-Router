"""
MiniMax / MiMo family contract.

Shares the switch-word reasoning shape with GLM today (both were served
by the same `is_chinese_m` branch in the legacy cascade), but is kept as
a SEPARATE file on purpose: when MiniMax changes its contract, the edit
must not risk GLM's behavior. The small amount of duplicated shape is the
cost of that isolation and is intentional.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import coerce_effort

SOURCE = "families/minimax.py"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    if ctx.effort == "enable":
        return prov.apply(
            payload, contract, "switch_enable", {"thinking": {"type": "enabled"}}
        )

    if ctx.effort == "adaptive":
        return prov.apply(
            payload, contract, "switch_adaptive", {"thinking": {"type": "adaptive"}}
        )

    oc = payload.get("output_config", {})
    if not isinstance(oc, dict):
        oc = {}
    oc["effort"] = coerce_effort(ctx.effort)
    return prov.apply(
        payload,
        contract,
        "enabled_with_effort",
        {"thinking": {"type": "enabled"}, "output_config": oc},
    )


CONTRACTS = [
    Contract(
        id="minimax",
        source=SOURCE,
        priority=40,
        pattern=r"minimax|mimo",
        apply=_apply,
    ),
]
