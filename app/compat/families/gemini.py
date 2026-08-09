"""
Gemini family contracts.

TWO axes of variation, which the legacy cascade conflated:

  AXIS 1 — model generation (what the setting MEANS):
    - 3.x  -> thinkingLevel enum (low/medium/high/max)
    - 2.5  -> thinkingBudget numeric token count (-1 = dynamic)

  AXIS 2 — upstream transport (WHERE the setting goes on the wire):
    - format: gemini     -> generationConfig.thinkingConfig.{...}  (native)
    - format: openai     -> reasoning_effort                       (gateway)
    - format: anthropic  -> thinking.{type,budget_tokens}          (gateway)

Both axes are load-bearing. In config.yaml the SAME Gemini model is served
over all three transports (vsllm-g = native gemini, antigravity/cursor/
github = openai-compatible, vietapi-a/hcnsec-vip = anthropic-shaped).
Emitting the native generationConfig to an OpenAI-compatible gateway is a
400, and vice versa.

The legacy cascade got both axes wrong: it emitted Anthropic-shaped keys
(thinking_config.budget_tokens) and a top-level thinkingLevel to EVERY
Gemini model regardless of transport, so native-gemini requests carried
fields Google rejects while the gateway requests carried fields the
gateway ignores.

The 3.x contract must outrank the generic one, since the generic pattern
also matches 3.x model ids.
"""
from __future__ import annotations

from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/gemini.py"

# Budget vocabulary -> exact token count (preferred operator vocabulary).
_BUDGET_MAP = {"16k": 16384, "32k": 32768, "64k": 65536, "128k": 131072}

# Coarse effort-word -> token budget, for 2.5 which has no enum.
# Google accepts any positive int or -1 (dynamic); these ensure 'high'
# actually enables thinking instead of silently no-oping as it did before.
_EFFORT_BUDGET_MAP = {
    "low": 8192,
    "medium": 24576,
    "high": 32768,
    "max": 65536,
    "xhigh": 65536,
}

# Gemini 3.x accepts these thinkingLevel enum values.
_VALID_LEVELS = {"low", "medium", "high", "max"}

# Effort words that are not levels, mapped onto the nearest valid level
# so a config value like "32k" on a 3.x model still means something.
_LEVEL_FALLBACK = {
    "xhigh": "max",
    "16k": "low",
    "32k": "medium",
    "64k": "high",
    "128k": "max",
}

_NATIVE = "gemini"
_ANTHROPIC = "anthropic"


def _resolve_budget(effort: str) -> int:
    """Effort vocabulary -> numeric token budget. -1 means dynamic."""
    budget = _BUDGET_MAP.get(effort, 0)
    if budget <= 0:
        budget = _EFFORT_BUDGET_MAP.get(effort, 0)
    if budget <= 0:
        # Unknown vocabulary ('enable', 'adaptive', ...) -> let the model
        # decide, rather than silently sending nothing as legacy did.
        budget = -1
    return budget


def _resolve_level(effort: str) -> str:
    """Effort vocabulary -> thinkingLevel enum."""
    if effort in _VALID_LEVELS:
        return effort
    return _LEVEL_FALLBACK.get(effort, "high")


def _set_native_thinking_config(
    payload: Dict[str, Any], values: Dict[str, Any]
) -> Dict[str, Any]:
    """Write into payload.generationConfig.thinkingConfig, preserving any
    sampling params a caller already placed in generationConfig."""
    gc = payload.get("generationConfig")
    if not isinstance(gc, dict):
        gc = {}
        payload["generationConfig"] = gc
    tc = gc.get("thinkingConfig")
    if not isinstance(tc, dict):
        tc = {}
        gc["thinkingConfig"] = tc
    tc.update(values)
    return payload


def _apply_gemini3(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    level = _resolve_level(ctx.effort)

    if ctx.wire_format == _NATIVE:
        payload = _set_native_thinking_config(
            payload, {"thinkingLevel": level, "includeThoughts": True}
        )
        return prov.apply(
            payload, contract, "thinking_level_native",
            {"generationConfig": payload["generationConfig"]},
        )

    if ctx.wire_format == _ANTHROPIC:
        return prov.apply(
            payload, contract, "thinking_level_anthropic",
            {"thinking": {"type": "enabled",
                          "budget_tokens": _resolve_budget(ctx.effort)}},
        )

    # OpenAI-compatible gateway.
    return prov.apply(
        payload, contract, "thinking_level_openai",
        {"reasoning_effort": level},
    )


def _apply_gemini_legacy(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    budget = _resolve_budget(ctx.effort)

    if ctx.wire_format == _NATIVE:
        payload = _set_native_thinking_config(payload, {"thinkingBudget": budget})
        return prov.apply(
            payload, contract, "budget_tokens_native",
            {"generationConfig": payload["generationConfig"]},
        )

    if ctx.wire_format == _ANTHROPIC:
        return prov.apply(
            payload, contract, "budget_tokens_anthropic",
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
        )

    # OpenAI-compatible gateway: no budget concept, degrade to an effort word.
    return prov.apply(
        payload, contract, "budget_tokens_openai",
        {"reasoning_effort": _resolve_level(ctx.effort)},
    )


def _sanitize_gemini(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    """Remove thinking fields that do not belong on THIS transport.

    Unconditional (runs even when thinking is off), because a stale field
    from an upstream layer is a 400 regardless of the current setting.
    """
    if ctx.wire_format == _NATIVE:
        # Native Google rejects unknown top-level fields.
        stale = ("thinking", "thinking_config", "reasoning_effort",
                 "reasoning", "output_config", "thinkingLevel")
    elif ctx.wire_format == _ANTHROPIC:
        stale = ("thinking_config", "reasoning_effort", "reasoning",
                 "thinkingLevel", "generationConfig", "includeThoughts")
    else:
        stale = ("thinking", "thinking_config", "output_config",
                 "thinkingLevel", "generationConfig", "includeThoughts")

    removed = {k: None for k in stale if k in payload}
    if removed:
        payload = prov.apply(payload, contract, "strip_wrong_transport", removed)
    return payload


CONTRACTS = [
    Contract(
        id="gemini-3",
        source=SOURCE,
        priority=90,
        pattern=r"gemini.*3",
        apply=_apply_gemini3,
        sanitize=_sanitize_gemini,
    ),
    Contract(
        id="gemini-legacy",
        source=SOURCE,
        priority=85,
        pattern=r"gemini",
        exclude=r"gemini.*3",
        apply=_apply_gemini_legacy,
        sanitize=_sanitize_gemini,
    ),
]
