"""
xAI Grok contract / Hợp đồng gia đình xAI Grok.

Grok 4.x is OpenAI-compatible and tunes depth via a top-level
`reasoning_effort`. Reasoning is mandatory, so there is no disable path.
The explicit *-non-reasoning SKU is excluded so effort is never injected
into a model with no reasoning engine.

Effort vocabulary differs by version / Bộ từ vựng effort khác nhau theo phiên bản:

  Grok 4.5              -> low / medium / high (NO xhigh — coerced to "high").
  Grok 4.6+             -> low / medium / high / xhigh (all four accepted).
  Grok 4.20-multi-agent -> same four words; effort = agent count, same wire field.

Per xAI docs: "xhigh is available on grok-4.6 and later. On models that do
not support it, such as grok-4.5, requests with xhigh are treated as high."
Default effort is high. Unknown/unsupported effort words must NOT pass
through — the vendor rejects them — so they coerce to the documented
default "high".

presence_penalty, frequency_penalty, and stop CANNOT be used with reasoning
models; upstream returns an error if present. All contracts matched here are
reasoning SKUs (non-reasoning is excluded), so sanitize always strips them.

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
_GROK_BASE_EFFORTS = ("low", "medium", "high")

# Reasoning models reject these; strip unconditionally in sanitize.
_REASONING_FORBIDDEN = ("presence_penalty", "frequency_penalty", "stop")


def _supports_xhigh(f_val: str) -> bool:
    """Return True if *f_val* denotes a Grok version that accepts xhigh."""
    m = re.search(r"grok[-_. ]*(\d+)(?:[.-](\d+))?", f_val, re.IGNORECASE)
    if not m:
        return False
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    return (major, minor) >= (4, 6)


def _coerce_grok_effort(ctx: ThinkingContext) -> str:
    """Coerce effort for Grok version-specific vocabularies.
    / Chuyển đổi effort theo bộ từ vựng riêng của từng phiên bản Grok.

    Unknown words coerce to "high" (documented default) — never pass through,
    because the vendor rejects unsupported effort values.
    """
    e = ctx.effort
    if e == "xhigh":
        # 4.6+ keeps xhigh; 4.5 and earlier coerce to high.
        if _supports_xhigh(ctx.f_val):
            return e
        return "high"
    if e in _GROK_BASE_EFFORTS:
        return e
    # Unknown / unsupported (none, off, banana, 32k, ...) -> default high.
    return "high"


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


def _sanitize(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Strip params that reasoning Grok models reject with an upstream error."""
    removals: Dict[str, Any] = {k: None for k in _REASONING_FORBIDDEN}
    return prov.apply(payload, contract, "strip_reasoning_incompatible", removals)


CONTRACTS = [
    Contract(
        id="grok",
        source=SOURCE,
        priority=65,
        pattern=r"grok|xai",
        exclude=r"non-reasoning",
        apply=_apply,
        sanitize=_sanitize,
        # Reasoning cannot be disabled; none/off/auto must still emit the
        # default effort ("high") rather than leaving the field absent.
        always_applies=True,
    ),
]
