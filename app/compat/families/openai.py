"""
OpenAI family contracts.

GPT-5.x uses a top-level `reasoning_effort` for /chat/completions, with
nested reasoning.mode / reasoning.context as best-effort extras for
reseller channels. effort="auto" is never emitted at either level.

`always_applies=True` because explicit reasoning_mode/reasoning_context
metadata must still be sent when no effort level is selected.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import apply_gpt5_reasoning_controls

SOURCE = "families/openai.py"


def _apply_gpt5(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    before_effort = payload.get("reasoning_effort")
    before_reasoning = payload.get("reasoning")

    payload = apply_gpt5_reasoning_controls(
        payload, ctx.effort, ctx.reasoning_mode, ctx.reasoning_context
    )

    # Attribute only what actually changed so the log stays truthful.
    changed: Dict[str, Any] = {}
    if payload.get("reasoning_effort") != before_effort:
        changed["reasoning_effort"] = payload.get("reasoning_effort")
    if payload.get("reasoning") != before_reasoning:
        changed["reasoning"] = payload.get("reasoning")
    if changed:
        prov.apply(payload, contract, "gpt5_reasoning_controls", changed)
    return payload


def _gpt5_applies(ctx: ThinkingContext) -> bool:
    return ctx.effort_is_explicit or ctx.reasoning_mode in ("standard", "pro") or (
        ctx.reasoning_context in ("auto", "current_turn", "all_turns")
    )


CONTRACTS = [
    Contract(
        id="gpt-5",
        source=SOURCE,
        priority=100,
        pattern=r"gpt-?5",
        apply=_apply_gpt5,
        # Metadata-only requests (mode/context without effort) must still
        # reach the upstream, so this contract opts out of the effort gate.
        always_applies=True,
    ),
]
