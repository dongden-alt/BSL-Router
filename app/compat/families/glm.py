"""
GLM (Zhipu) family contract.

Effort vocabulary differs BY VERSION within the family, which is the
churn this refactor is designed for:

  GLM-5.3  -> ONLY three accepted words: low / high / max.
               ANY other wire value is an upstream error.
               Coding-plan mapping into that vocab:
                 none/minimal/low -> low
                 medium/high      -> high
                 xhigh/max        -> max
               Unknown leftovers coerce to high (safe default).

  GLM-5.2  -> accepts max (default), xhigh, high, medium, low, minimal, none.
               Official semantics:
                 none/minimal = model stops thinking
                   -> wire as thinking {type: disabled} WITHOUT reasoning_effort
                      (disabled is the wire-off signal; vendor docs say
                      none/minimal mean the model stops thinking)
                 low/medium   -> high
                 high         -> high
                 xhigh        -> max
                 max          -> max
               Unknown leftovers coerce to high.

  5.1/5.x  -> "enable" / "adaptive" switch words; anything else degrades
               to enabled + output_config.effort.

Kept as ONE contract with an internal branch rather than two, because
GLM-5.2 with a switch word ("enable") must still fall through to the
generic behavior — splitting into two contracts would make the 5.2
contract win and silently drop that path.

`always_applies` is True so the graded path can see the vendor word
"none" (which is otherwise treated as an OFF_VALUE and would skip apply).
auto/off/"" still no-op inside _apply — only real vocabulary is written.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import coerce_effort

SOURCE = "families/glm.py"

# Versions that accept graded effort words rather than switch words.
_GRADED_EFFORT_VERSIONS = r"glm-5\.[23]"
# GLM-5.3 native wire vocabulary — only these three words are accepted upstream.
_GLM53_EFFORT_WORDS = ("low", "high", "max")


def _is_glm53(f_val: str) -> bool:
    return bool(re.search(r"glm-5\.3", f_val, re.IGNORECASE))


def _is_glm52(f_val: str) -> bool:
    return bool(re.search(r"glm-5\.2", f_val, re.IGNORECASE))


def _coerce_glm53_effort(effort: str) -> str:
    """Map any effort into GLM-5.3's three-word vocabulary (low/high/max)."""
    if effort in ("none", "minimal", "low"):
        return "low"
    if effort in ("medium", "high"):
        return "high"
    if effort in ("xhigh", "max"):
        return "max"
    # Garbage / budget words / anything else -> high (safe default).
    return "high"


def _coerce_glm52_effort(effort: str) -> str:
    """Map on-thinking effort into GLM-5.2 effective levels (high/max).

    none/minimal are handled by the caller (thinking off) — they never
    reach this helper.
    """
    if effort in ("low", "medium", "high"):
        return "high"
    if effort in ("xhigh", "max"):
        return "max"
    return "high"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    graded = bool(re.search(_GRADED_EFFORT_VERSIONS, ctx.f_val, re.IGNORECASE))

    # auto/off/"" = operator did not pick a level. Leave payload alone.
    # "none" is in OFF_VALUES globally but is a real vendor vocabulary word
    # for graded GLM — it must fall through to the version-specific branch.
    if not ctx.effort_is_explicit and not (graded and ctx.effort == "none"):
        return payload

    if graded and ctx.effort not in ("enable", "adaptive"):
        # ── GLM-5.2: none/minimal stop thinking ──────────────────────────
        # Vendor docs: none/minimal mean the model stops thinking.
        # Assumption: wire-off signal is thinking {type: disabled} and we
        # omit reasoning_effort (a depth knob is meaningless when off).
        if _is_glm52(ctx.f_val) and ctx.effort in ("none", "minimal"):
            return prov.apply(
                payload,
                contract,
                "graded_thinking_off",
                {
                    "thinking": {"type": "disabled"},
                    "reasoning_effort": None,
                },
            )

        if _is_glm52(ctx.f_val):
            effort = _coerce_glm52_effort(ctx.effort)
        else:
            # GLM-5.3 (and any future graded match that isn't 5.2):
            # result MUST be one of the three accepted words.
            effort = _coerce_glm53_effort(ctx.effort)

        return prov.apply(
            payload,
            contract,
            "graded_effort",
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            },
        )

    if ctx.effort == "enable":
        return prov.apply(
            payload, contract, "switch_enable", {"thinking": {"type": "enabled"}}
        )

    if ctx.effort == "adaptive":
        return prov.apply(
            payload, contract, "switch_adaptive", {"thinking": {"type": "adaptive"}}
        )

    # For graded versions: let everything else fall through to generic
    # enabled+output_config (legacy compat).  Non-graded GLM always uses
    # this path.
    oc = payload.get("output_config", {})
    if not isinstance(oc, dict):
        oc = {}
    oc["effort"] = coerce_effort(ctx.effort)
    return prov.apply(
        payload,
        contract,
        "enabled_with_effort",
        {"thinking": {"type": "enabled"}, "output_config": oc},
    )


CONTRACTS = [
    Contract(
        id="glm",
        source=SOURCE,
        priority=40,
        # Hyphen is intentional: matches glm-5.1 / glm-5.2 model ids.
        pattern=r"glm-",
        apply=_apply,
        # So graded "none" (an OFF_VALUE) still reaches version-specific mapping.
        always_applies=True,
    ),
]
