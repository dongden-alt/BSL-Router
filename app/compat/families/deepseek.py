"""
DeepSeek family contract.

DeepSeek V4 accepts Anthropic-style `thinking` and top-level
`reasoning_effort` across the reseller channels it is served on.

Historically this contract also emitted `output_config.effort` (the
"triple_shape") because some channels only honor that container. Live
evidence 2026-08-20 against x5m5x / Chinese OpenAI-compatible gateways:

  400 invalid_request_error: "未知请求字段：output_config"
  (unknown request field: output_config)

Those gateways treat unknown top-level fields as hard errors, and the
thinking-fallback detector previously missed the Chinese phrasing so
BSL never degraded-and-retried. Prefer the dual shape
(`thinking` + `reasoning_effort`) which both official DeepSeek and the
reseller channels accept; strip any inherited `output_config`.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/deepseek.py"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    # Dual shape: thinking + reasoning_effort. Explicitly delete
    # output_config so a stale upstream layer cannot reintroduce the
    # field that Chinese resellers 400 on.
    return prov.apply(
        payload,
        contract,
        "dual_shape",
        {
            "thinking": {"type": "enabled"},
            "reasoning_effort": ctx.effort,
            "output_config": None,
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
