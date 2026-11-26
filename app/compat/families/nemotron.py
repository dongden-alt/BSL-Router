"""
NVIDIA Nemotron reasoning family contract (`nemotron-3-ultra*`, `nemotron-3.5-super*`,
`nemotron-3-super*`, `nemotron-3-nano*`, `nemotron-3.5-lightning*`, `nemotron-ultra*`).

Live-verified against `crowllm/nemotron-3-ultra-550b-a55b-uncensored:free`,
`crowllm/nemotron-3.5-super-vl:free`, and
`crowllm/nvidia/nemotron-3.5-super-vl-120b-a12b:free` (2026-11-26):
  - Accepts OpenAI-style top-level `reasoning_effort` with graduated
    vocabulary: `none` / `low` / `medium` / `high` / `xhigh`.
  - Upstream vLLM does not recognize `"max"` as a tier above `"high"`/`"xhigh"`
    and silently falls back to a minimal 63-token default budget when `"max"`
    is sent; therefore `"max"` / `"ultra"` clamp to `"high"` server-side
    (matching StepFun / Grok precedent).
  - Non-reasoning SKUs (`nemotron-3.5-bolt-e4b`, `nemotron-3.5-content-safety`,
    guard/embed/reward/parse/rerank) are explicitly excluded so they do not
    receive reasoning parameters or a thinking dropdown in the UI.
  - Sets `always_applies=True` so an explicit UI `off` emits
    `reasoning_effort="none"` (on OpenAI wire) or `thinking={"type":"disabled"}`
    (on Anthropic wire), while `auto` / `""` leaves the payload untouched so
    the provider default stands.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/nemotron.py"

_VALID_EFFORTS = ("low", "medium", "high", "xhigh")
_DISABLE_WORDS = ("off", "none", "false", "disable", "disabled")
_UNSET_WORDS = ("", "auto")
_BUDGET_RE = re.compile(r"^\d+[km]?$")


def _normalize_effort(raw: Any) -> str:
    """Normalize an explicit on-thinking effort into Nemotron's vocabulary."""
    v = str(raw or "").strip().lower()
    if v in _VALID_EFFORTS:
        return v
    if v == "minimal":
        return "low"
    if _BUDGET_RE.match(v):
        return "high"
    # max / ultra / enable / adaptive / unknown -> clamp to 'high' (avoid vLLM max fallback).
    return "high"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    effort = str(ctx.effort or "").strip().lower()

    if effort in _UNSET_WORDS:
        return payload

    if ctx.wire_format == "anthropic":
        if effort in _DISABLE_WORDS:
            return prov.apply(
                payload,
                contract,
                "thinking_off_anthropic",
                {
                    "thinking": {"type": "disabled"},
                    "reasoning_effort": None,
                    "output_config": None,
                },
            )
        lvl = _normalize_effort(effort)
        return prov.apply(
            payload,
            contract,
            "output_config_effort",
            {
                "thinking": {"type": "enabled"},
                "output_config": {"effort": lvl},
                "reasoning_effort": None,
            },
        )

    if ctx.wire_format == "openai":
        if effort in _DISABLE_WORDS:
            return prov.apply(
                payload,
                contract,
                "reasoning_effort_none",
                {
                    "reasoning_effort": "none",
                    "thinking": None,
                    "output_config": None,
                },
            )
        lvl = _normalize_effort(effort)
        rule = "reasoning_effort" if lvl == effort else "reasoning_effort_normalized"
        return prov.apply(
            payload,
            contract,
            rule,
            {
                "reasoning_effort": lvl,
                "thinking": None,
                "output_config": None,
            },
        )

    return payload


CONTRACTS = [
    Contract(
        id="nemotron-3",
        source=SOURCE,
        priority=60,
        pattern=r"\bnemotron\b",
        exclude=r"bolt|content-safety|guard|embed|reward|parse|rerank",
        apply=_apply,
        always_applies=True,
    ),
]
