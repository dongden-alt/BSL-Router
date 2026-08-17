"""
ByteDance Doubao (Ark/Volcengine) family contract / Hợp đồng gia đình ByteDance Doubao.

Doubao 2.0 Pro is served via Volcengine Ark and accepts `reasoning_effort`
with the following vocabulary: `minimal` / `low` / `medium` / `high`.
Default is `medium`. `minimal` disables thinking entirely (answer directly).

This is NOT the standard OpenAI vocabulary — Doubao uses `minimal` where
OpenAI uses `none`/`off`. Effort values like `max` or `xhigh` that are not
in Doubao's vocabulary are coerced to the nearest valid value.

Doubao 2.0 Pro được phục vụ qua Volcengine Ark và chấp nhận `reasoning_effort`
với bộ từ vựng: `minimal` / `low` / `medium` / `high`. Mặc định là `medium`.
`minimal` tắt thinking hoàn toàn (trả lời trực tiếp).

Đây KHÔNG phải bộ từ vựng OpenAI chuẩn — Doubao dùng `minimal` thay vì
`none`/`off`. Các giá trị như `max` hoặc `xhigh` không thuộc bộ từ vựng
của Doubao sẽ được chuyển đổi sang giá trị gần nhất.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import (
    OFF_VALUES,
    Contract,
    Provenance,
    ThinkingContext,
)

SOURCE = "families/doubao.py"

# Doubao's own vocabulary (NOT standard OpenAI).
_DOUBAO_EFFORTS = ("minimal", "low", "medium", "high")


def _coerce_doubao_effort(ctx: ThinkingContext) -> str:
    """Map any effort value into Doubao's vocabulary.
    / Chuyển đổi mọi giá trị effort sang bộ từ vựng của Doubao.
    """
    e = ctx.effort
    if e in _DOUBAO_EFFORTS:
        return e
    # OFF_VALUES (auto/none/off) -> minimal (disable thinking, answer directly).
    if not ctx.effort_is_explicit:
        return "minimal"
    # max, xhigh, or anything unknown -> high (highest available).
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
        {"reasoning_effort": _coerce_doubao_effort(ctx)},
    )


CONTRACTS = [
    Contract(
        id="doubao",
        source=SOURCE,
        priority=50,
        pattern=r"doubao",
        apply=_apply,
        # Send minimal even when thinking is off so reasoning is
        # explicitly disabled, not left to the model's default.
        always_applies=True,
    ),
]
