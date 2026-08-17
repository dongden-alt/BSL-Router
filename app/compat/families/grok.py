"""
xAI Grok contract / Hợp đồng gia đình xAI Grok.

Grok 4.x is OpenAI-compatible and tunes depth via a top-level
`reasoning_effort`. Reasoning is mandatory, so there is no disable path.
The explicit *-non-reasoning SKU is excluded so effort is never injected
into a model with no reasoning engine.

Effort vocabulary differs by version / Bộ từ vựng effort khác nhau theo phiên bản:

  Grok 4.5    -> low / medium / high (NO xhigh — coerced to "high").
  Grok 4.6+   -> low / medium / high / xhigh (all four accepted, xhigh is new).

Per xAI docs: "xhigh is available on grok-4.6 and later. On models that do
not support it, such as grok-4.5, requests with xhigh are treated as high."

Grok 4.x tương thích OpenAI và điều chỉnh độ sâu suy luận qua tham số
`reasoning_effort` ở cấp độ top-level. Suy luận là bắt buộc, không có
đường tắt tắt suy luận. SKU *-non-reasoning bị loại trừ.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/grok.py"

# Grok 4.6+ supports xhigh; 4.5 and earlier do NOT.
_GROK_XHIGH_OK = r"grok.*4[.-][6-9]|grok.*[5-9]"
_GROK_BASE_EFFORTS = ("low", "medium", "high")


def _coerce_grok_effort(ctx: ThinkingContext) -> str:
    """Coerce effort for Grok version-specific vocabularies.
    / Chuyển đổi effort theo bộ từ vựng riêng của từng phiên bản Grok.
    """
    e = ctx.effort
    if e == "xhigh":
        # 4.6+ keeps xhigh; 4.5 and earlier coerce to high.
        if bool(re.search(_GROK_XHIGH_OK, ctx.f_val, re.IGNORECASE)):
            return e
        return "high"
    if e in _GROK_BASE_EFFORTS:
        return e
    # Unknown effort values — pass through (operator may know something we don't).
    return e


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    return prov.apply(
        payload,
        contract,
        "reasoning_effort",
        {"reasoning_effort": _coerce_grok_effort(ctx)},
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
