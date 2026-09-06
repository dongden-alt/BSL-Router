"""
OpenAI family contracts.

GPT-5.x / GPT-6 (Astra) use a top-level `reasoning_effort` for /chat/completions,
with nested reasoning.mode / reasoning.context as best-effort extras for
reseller channels. effort="auto" is never emitted at either level. gpt-6-astra
carries no mode/context config keys, so those extras never fire for it, and
budget-style thinking values (e.g. "32k") coerce to a level before emission.

`always_applies=True` because explicit reasoning_mode/reasoning_context
metadata must still be sent when no effort level is selected.

Transport note (2026-08-20): on an Anthropic-compatible wire
(`wire_format=anthropic`), OpenAI-style `reasoning_effort` / `reasoning`
must NOT be emitted. Live AgentRouter probe for gpt-5.6-sol:
  - /v1/messages plain body -> content "OK"
  - /v1/messages + reasoning_effort/mode -> stalls after message_start (empty)
  - /v1/chat/completions + reasoning_effort/mode -> content "OK"
So on the anthropic wire the contract strips foreign keys and is a no-op.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import apply_gpt5_reasoning_controls, coerce_effort

SOURCE = "families/openai.py"

# OpenAI-native reasoning keys that Anthropic-compatible gateways reject
# or silently mishandle when a GPT model is served over /v1/messages.
_OPENAI_REASONING_KEYS = (
    "reasoning_effort",
    "reasoning",
    "output_config",
    "thinking",
)


def _apply_gpt5(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    # Anthropic wire: never emit OpenAI reasoning controls. Reseller
    # gateways that re-expose GPT models on /v1/messages stall empty when
    # these fields arrive (AgentRouter gpt-5.6-sol, 2026-08-20).
    if ctx.wire_format == "anthropic":
        strip_vals = {k: None for k in _OPENAI_REASONING_KEYS if k in payload}
        if strip_vals:
            return prov.apply(
                payload, contract, "anthropic_wire_strip_openai_reasoning", strip_vals
            )
        return payload

    before_effort = payload.get("reasoning_effort")
    before_reasoning = payload.get("reasoning")

    # Budget-style thinking values (e.g. "32k") coerce to a level here so an
    # invalid effort never reaches the wire; real effort words pass through
    # unchanged and auto stays auto (emits nothing).
    payload = apply_gpt5_reasoning_controls(
        payload, coerce_effort(ctx.effort), ctx.reasoning_mode, ctx.reasoning_context
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


CONTRACTS = [
    Contract(
        id="gpt-5",
        source=SOURCE,
        priority=100,
        # [56] pulls gpt-6 (Astra) onto the same effort contract. Astra is
        # effort-only: no reasoning_mode/reasoning_context config keys exist
        # for it, so the mode/context extras below stay unset naturally.
        pattern=r"gpt-?[56]",
        apply=_apply_gpt5,
        # Metadata-only requests (mode/context without effort) must still
        # reach the upstream, so this contract opts out of the effort gate.
        always_applies=True,
    ),
]
