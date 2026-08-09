"""
BSL Router — Shared effort/budget coercion helpers.

Moved verbatim from app/main.py so family contracts can use them without
importing main (which would be circular). main.py re-exports these names,
so existing imports and tests continue to work unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


def coerce_effort(value: Any) -> str:
    """Coerce a thinking value into a valid reasoning EFFORT LEVEL.

    `output_config.effort` / `reasoning.effort` accept effort LEVELS
    (low/medium/high/max, plus xhigh on some channels) — never a token budget.
    A budget-style config value like "32k" (or a raw token count) otherwise
    leaks straight through as an invalid effort; Anthropic-compatible origins
    (e.g. pix4k opus) reject the malformed body. Map budget forms to a level
    by size (<=16k -> medium, else max) and pass real effort words through
    unchanged so working values (max/high/xhigh/adaptive) are never altered.
    """
    v = str(value or "").strip().lower()
    if not v:
        return v
    m = re.fullmatch(r"(\d+)\s*k?", v)
    if m:
        n = int(m.group(1))
        if v.endswith("k"):
            n *= 1024
        return "medium" if n <= 16384 else "max"
    return v


def budget_tokens(value: Any) -> Optional[int]:
    """Return an int token budget if `value` is a budget-style thinking config
    (e.g. "32k" -> 32768, "32768" -> 32768), else None.

    Used to route BUDGET-configured Claude models onto the explicit
    `thinking:{type:enabled, budget_tokens:N}` contract instead of the
    `adaptive + output_config.effort` (level) contract. Per operator policy
    only Opus 4.6 antigravity* SKUs are configured with a budget (thinking:32k);
    every other Opus/Sonnet uses an effort level. A bare tiny integer is not a
    budget (guards against a stray "2" etc.).
    """
    v = str(value or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*(k?)", v)
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2) == "k":
        n *= 1024
    return n if n >= 1024 else None


def claude_modern_thinking(f_val: str, thinking_suffix: Any) -> Tuple[Dict[str, Any], Optional[str], Optional[int]]:
    """Resolve the reasoning contract for a modern (Claude-4) model.

    Returns (thinking_obj, effort_level_or_None, budget_or_None):
      - Opus 4.6 antigravity* configured with a token budget (thinking:32k)
        -> ({"type":"enabled","budget_tokens":N}, None, N). These are the ONLY
        Claude SKUs the operator configures with a budget.
      - Every other Opus/Sonnet (thinking:max|high|...)
        -> ({"type":"adaptive"}, "<level>", None). Opus 4.7+/4.8+ reject
        "enabled" (400), so they MUST stay on the adaptive+effort path.

    Per explicit operator policy the budget path is gated on THREE conditions:
    the antigravity SKU marker AND the 4.6 version AND a budget-style value.
    "Only opus 4.6 antigravity* use the 32k budget; every other Opus/Sonnet
    uses a level." So a non-antigravity opus-4.6 (even if mis-configured with
    thinking:32k) and any 4.7/4.8 SKU stay on the adaptive+effort level path,
    where the 32k is coerced to a valid level rather than forcing the
    enabled+budget shape that 4.7/4.8 reject with a 400.
    Pure/deterministic so it can be unit-tested without the full handler.
    """
    fv = f_val or ""
    is_opus_46_antigravity = bool(
        re.search(r'opus.*4[.-]6', fv) and re.search(r'antigravity', fv)
    )
    budget = budget_tokens(thinking_suffix) if is_opus_46_antigravity else None
    if budget is not None:
        return ({"type": "enabled", "budget_tokens": budget}, None, budget)
    return ({"type": "adaptive"}, coerce_effort(thinking_suffix), None)


def apply_gpt5_reasoning_controls(payload: dict, effort, mode, context) -> dict:
    """Apply explicit GPT-5 reasoning controls without ever emitting effort=auto."""
    explicit_effort = str(effort or "").lower() not in ("auto", "none", "off", "")
    valid_mode = mode if mode in ("standard", "pro") else None
    valid_context = context if context in ("auto", "current_turn", "all_turns") else None
    if not explicit_effort and not valid_mode and not valid_context:
        return payload

    if explicit_effort:
        payload["reasoning_effort"] = str(effort).lower()
    reasoning = payload.get("reasoning", {})
    if not isinstance(reasoning, dict):
        reasoning = {}
    if explicit_effort:
        reasoning["effort"] = str(effort).lower()
    else:
        reasoning.pop("effort", None)
    if valid_mode:
        reasoning["mode"] = valid_mode
    if valid_context:
        reasoning["context"] = valid_context
    if reasoning:
        payload["reasoning"] = reasoning
    return payload
