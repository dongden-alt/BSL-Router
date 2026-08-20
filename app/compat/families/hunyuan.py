"""
Tencent Hunyuan family contract / Hợp đồng gia đình Tencent Hunyuan.

Hunyuan Hy3 is a 295B MoE model (21B active) served via vLLM or SGLang
with an OpenAI-compatible API. Reasoning is controlled via `reasoning_effort`
nested inside `chat_template_kwargs` — NOT as a top-level parameter.

Effort vocabulary: `no_think` (default, direct response), `low` (light
reasoning), `high` (deep chain-of-thought / complex logic/math/coding).
`medium` and `max` are NOT valid and must be coerced.

Recommended generation settings (official, fill-when-absent only):
    temperature=0.9, top_p=1.0

Per the official Tencent HuggingFace model card, the canonical invocation is:
    extra_body={"chat_template_kwargs": {"reasoning_effort": "no_think"}}

The `extra_body` in the OpenAI SDK flattens to top-level in the HTTP body,
so BSL Router must write `chat_template_kwargs` as a top-level key.

`chat_template_kwargs` is registered in THINKING_PAYLOAD_KEYS so the
thinking-fallback middleware can strip it during degrade-and-retry.

Hunyuan Hy3 là model MoE 295B (21B active) được phục vụ qua vLLM hoặc
SGLang với API tương thích OpenAI. Suy luận được điều khiển qua
`reasoning_effort` nằm bên trong `chat_template_kwargs` — KHÔNG phải
tham số top-level.

Bộ từ vựng: `no_think` (mặc định, trả lời trực tiếp), `low` (suy luận nhẹ),
`high` (deep chain-of-thought). `medium` và `max` không hợp lệ và phải
chuyển đổi.

Cài đặt sinh token khuyến nghị (chính thức, chỉ điền khi vắng):
    temperature=0.9, top_p=1.0
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import (
    Contract,
    Provenance,
    ThinkingContext,
)

SOURCE = "families/hunyuan.py"

# Hy3 only accepts no_think, low, high (NO medium, NO max).
_HY_VALID_EFFORTS = ("no_think", "low", "high")

# Official recommended sampling defaults — fill only when the client/operator
# left the key unset. Never override an explicit value.
_OFFICIAL_SAMPLING_DEFAULTS: Dict[str, Any] = {
    "temperature": 0.9,
    "top_p": 1.0,
}


def _coerce_effort(ctx: ThinkingContext) -> str:
    """Map any effort value into Hy3's restricted vocabulary."""
    e = ctx.effort
    if e in _HY_VALID_EFFORTS:
        return e
    # OFF_VALUES (auto/none/off) -> no_think (disable reasoning, direct response).
    if not ctx.effort_is_explicit:
        return "no_think"
    # medium, max, xhigh, or anything else -> high (highest available).
    return "high"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    effort = _coerce_effort(ctx)
    # Hy3 expects reasoning_effort nested inside chat_template_kwargs
    # (per official Tencent HuggingFace model card).
    # Hy3 mong đợi reasoning_effort nằm bên trong chat_template_kwargs
    # (theo model card chính thức của Tencent trên HuggingFace).
    chat_kwargs = payload.get("chat_template_kwargs", {})
    if not isinstance(chat_kwargs, dict):
        chat_kwargs = {}
    chat_kwargs["reasoning_effort"] = effort
    return prov.apply(
        payload,
        contract,
        "chat_template_kwargs.reasoning_effort",
        {"chat_template_kwargs": chat_kwargs},
    )


def _sanitize(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Fill official sampling defaults for keys the client left unset.

    temperature=0.9 and top_p=1.0 are Tencent's recommended generation
    settings. Never override operator/client-supplied values.
    """
    fills = {k: v for k, v in _OFFICIAL_SAMPLING_DEFAULTS.items() if k not in payload}
    if fills:
        payload = prov.apply(payload, contract, "official_sampling_defaults", fills)
    return payload


CONTRACTS = [
    Contract(
        id="hunyuan-hy3",
        source=SOURCE,
        priority=50,
        pattern=r"hunyuan|hy3",
        apply=_apply,
        sanitize=_sanitize,
        # Send no_think even when thinking is off so reasoning is
        # explicitly disabled, not left to the model's default.
        always_applies=True,
    ),
]
