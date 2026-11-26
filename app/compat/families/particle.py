"""
Particle family contract (particle-1, particle-1-uncensored, etc.).

Live-verified against `crowllm/particle-1-uncensored:free` (2026-11-25):
  - Accepts OpenAI-style top-level `reasoning_effort` with full graduated
    vocabulary: `none` / `low` / `medium` / `high` / `xhigh` / `max`
    (all 6 return HTTP 200 with monotonic reasoning-token scaling).
  - Because the upstream thinks by default when `reasoning_effort` is omitted,
    this contract sets `always_applies=True` so an explicit UI `off` emits
    `reasoning_effort="none"` (on OpenAI wire) or `thinking={"type":"disabled"}`
    (on Anthropic wire), while `auto` / `""` leaves the payload untouched so
    the provider default stands.
  - On OpenAI wire, inherited `thinking` / `output_config` containers are
    stripped (`None`) to prevent strict OpenAI-compatible gateways from
    returning HTTP 400 on unknown top-level fields.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext

SOURCE = "families/particle.py"

_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_DISABLE_WORDS = ("off", "none", "false", "disable", "disabled")
_UNSET_WORDS = ("", "auto")
_BUDGET_RE = re.compile(r"^\d+[km]?$")


def _normalize_effort(raw: Any) -> str:
    """Normalize an explicit on-thinking effort into particle's vocabulary."""
    v = str(raw or "").strip().lower()
    if v in _VALID_EFFORTS:
        return v
    if v == "minimal":
        return "low"
    if _BUDGET_RE.match(v):
        return "high"
    # enable / adaptive / unknown -> safe high default.
    return "high"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    effort = str(ctx.effort or "").strip().lower()

    # Never fabricate a preference the operator did not express.
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

    # Other transports (gemini / openai-responses): leave untouched.
    return payload


CONTRACTS = [
    Contract(
        id="particle-1",
        source=SOURCE,
        priority=60,
        pattern=r"\bparticle(?:[-_.]?\d+)?\b",
        apply=_apply,
        always_applies=True,
    ),
]
