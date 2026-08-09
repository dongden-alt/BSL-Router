"""
Anthropic family contracts — three incompatible generations.

  modern (Claude 4.x)  -> adaptive + output_config.effort, EXCEPT the
                          Opus 4.6 antigravity* SKUs which are the only
                          ones configured with an explicit token budget.
                          4.7+/4.8+ reject type="enabled" with a 400, so
                          they must stay on the adaptive+effort path.
  next   (Fable/Mythos)-> thinking {type: adaptive|enabled} + effort.
                          Computed BEFORE modern/legacy and excluded from
                          them so the relaxed version match cannot steal
                          fable-5 / mythos-5.
  legacy (Claude 3.x)  -> enabled + budget_tokens, or bare adaptive when
                          no budget vocabulary was configured.

Version detection deliberately allows a single-digit major (claude-sonnet-5)
because a mandatory ".Y" previously caused sonnet-5 to fall through the
entire cascade and ship with NO thinking at all.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import claude_modern_thinking, coerce_effort

SOURCE = "families/anthropic.py"

# Shared version matcher. Group 2 is the major version.
_VERSION_RE = r"(?:claude|opus|sonnet).*?(\d+)(?:[.-](\d+))?"
_NEXT_RE = r"fable|mythos"

_LEGACY_BUDGETS = {"16k": 16384, "32k": 32768, "64k": 65536, "128k": 131072}


def _major(f_val: str) -> int:
    m = re.search(_VERSION_RE, f_val, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _apply_modern(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    think, effort_level, budget = claude_modern_thinking(ctx.f_val, ctx.effort)

    if budget is not None:
        # Budget path: raise max_tokens to leave room for the reasoning
        # budget, and drop output_config (the two are mutually exclusive).
        new_max = max(int(payload.get("max_tokens", 0) or 0), budget + 32768)
        return prov.apply(
            payload,
            contract,
            "enabled_budget",
            {"thinking": think, "max_tokens": new_max, "output_config": None},
        )

    oc = payload.get("output_config", {})
    if not isinstance(oc, dict):
        oc = {}
    oc["effort"] = effort_level
    return prov.apply(
        payload,
        contract,
        "adaptive_effort",
        {"thinking": think, "output_config": oc},
    )


def _apply_next(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    mode = ctx.reasoning_mode if ctx.reasoning_mode in ("adaptive", "enabled") else "adaptive"
    oc = payload.get("output_config", {})
    if not isinstance(oc, dict):
        oc = {}
    oc["effort"] = coerce_effort(ctx.effort)
    return prov.apply(
        payload,
        contract,
        "extended_thinking",
        {"thinking": {"type": mode}, "output_config": oc},
    )


def _apply_legacy(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    budget = _LEGACY_BUDGETS.get(ctx.effort, 0)
    if budget > 0:
        new_max = max(int(payload.get("max_tokens", 0) or 0), budget + 32768)
        return prov.apply(
            payload,
            contract,
            "enabled_budget",
            {
                "thinking": {"type": "enabled", "budget_tokens": budget},
                "max_tokens": new_max,
            },
        )
    return prov.apply(
        payload, contract, "adaptive", {"thinking": {"type": "adaptive"}}
    )


class _VersionedContract(Contract):
    """Contract that additionally gates on the Claude major version.

    The version test cannot be expressed as a plain regex because it is a
    numeric comparison (>=4 for modern, ==3 for legacy) against a captured
    group, so it is applied as a post-filter on top of the pattern match.
    """

    def __init__(self, *args, major_min: int = 0, major_eq: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._major_min = major_min
        self._major_eq = major_eq

    def matches(self, ctx: ThinkingContext) -> bool:
        if not super().matches(ctx):
            return False
        major = _major(ctx.f_val)
        if not major:
            return False
        if self._major_eq:
            return major == self._major_eq
        return major >= self._major_min


CONTRACTS = [
    _VersionedContract(
        id="claude-modern",
        source=SOURCE,
        priority=80,
        pattern=_VERSION_RE,
        exclude=_NEXT_RE,
        apply=_apply_modern,
        major_min=4,
    ),
    Contract(
        id="claude-next",
        source=SOURCE,
        priority=75,
        pattern=_NEXT_RE,
        apply=_apply_next,
    ),
    _VersionedContract(
        id="claude-legacy",
        source=SOURCE,
        priority=70,
        pattern=_VERSION_RE,
        exclude=_NEXT_RE,
        apply=_apply_legacy,
        major_eq=3,
    ),
]
