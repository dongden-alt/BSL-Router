"""
Meta Muse Spark family contract / Hợp đồng gia đình Meta Muse Spark.

Muse Spark 1.1/1.2 is Meta's multimodal reasoning model with a 1M token
context window. It uses `reasoning_effort` with an extended vocabulary:
`minimal` / `low` / `medium` / `high` / `xhigh`. Default is `medium`.

Reasoning is always on and cannot be disabled — `minimal` is the lowest
level (not a full disable). Unsupported values like `max` are coerced.

Muse Spark 1.1/1.2 là model reasoning đa phương thức của Meta với cửa sổ
ngữ cảnh 1M token. Sử dụng `reasoning_effort` với bộ từ vựng mở rộng:
`minimal` / `low` / `medium` / `high` / `xhigh`. Mặc định là `medium`.

Suy luận luôn bật và không thể tắt — `minimal` là mức thấp nhất (không
phải tắt hoàn toàn). Các giá trị không hỗ trợ như `max` được chuyển đổi.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import (
    OFF_VALUES,
    Contract,
    Provenance,
    ThinkingContext,
)

SOURCE = "families/muse.py"

# Muse Spark's extended vocabulary (includes minimal AND xhigh).
_MUSE_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _coerce_muse_effort(ctx: ThinkingContext) -> str:
    """Map any effort value into Muse Spark's vocabulary.
    / Chuyển đổi mọi giá trị effort sang bộ từ vựng của Muse Spark.
    """
    e = ctx.effort
    if e in _MUSE_EFFORTS:
        return e
    # OFF_VALUES (auto/none/off) -> minimal (lowest level, reasoning still on).
    if not ctx.effort_is_explicit:
        return "minimal"
    # max or anything unknown -> xhigh (highest available).
    return "xhigh"


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
        {"reasoning_effort": _coerce_muse_effort(ctx)},
    )


CONTRACTS = [
    Contract(
        id="muse-spark",
        source=SOURCE,
        priority=48,
        # Match both hyphen and underscore variants.
        pattern=r"muse[\-_\s]?spark",
        apply=_apply,
        # Send minimal even when thinking is off — Muse reasoning is always
        # on, so we pick the lowest level instead of sending nothing.
        always_applies=True,
    ),
]
