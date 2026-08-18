"""
GLM (Zhipu) family contract.

Effort vocabulary differs BY VERSION within the family, which is the
churn this refactor is designed for:

  5.2/5.3  -> accepts graded effort words (low/high/max) alongside
               thinking {type: enabled}. NO "medium" — coerced to "high".
  5.1/5.x  -> "enable" / "adaptive" switch words; anything else degrades
               to enabled + output_config.effort.

Kept as ONE contract with an internal branch rather than two, because
GLM-5.2 with a switch word ("enable") must still fall through to the
generic behavior — splitting into two contracts would make the 5.2
contract win and silently drop that path.

Adding GLM-5.3: add a branch to `_apply` (or a new Contract if 5.3 is a
genuinely separate shape) and a row to the version table below. No other
file changes.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.compat.families._base import Contract, Provenance, ThinkingContext
from app.compat.families._effort import coerce_effort

SOURCE = "families/glm.py"

# Versions that accept graded effort words rather than switch words.
_GRADED_EFFORT_VERSIONS = r"glm-5\.[23]"
# GLM-5.2/5.3 accepts low/high/max. NO "medium" — will be coerced to "high".
_GRADED_EFFORT_WORDS = ("low", "high", "max")


def _coerce_glm_effort(ctx: ThinkingContext) -> str:
    """Coerce invalid effort values for GLM graded versions.
    / Chuyển đổi các giá trị effort không hợp lệ cho phiên bản GLM graded.

    GLM-5.2 has a "7-value compatibility mapping" (effective levels: max/high).
    GLM-5.3 narrows to three native levels: low/high/max.
    Mapping (per Pi issue #5770 + AIHubMix GLM-5.3 guide):
      medium  -> high   (default effective level for 5.2)
      xhigh   -> max    (per Pi issue #5770)
      others  -> high   (safe fallback)
    """
    e = ctx.effort
    if e in _GRADED_EFFORT_WORDS:
        return e
    # xhigh -> max (per Pi GitHub issue #5770 mapping: "xhigh: max").
    if e == "xhigh":
        return "max"
    # medium or anything unknown -> high.
    return "high"


def _apply(
    payload: Dict[str, Any],
    ctx: ThinkingContext,
    prov: Provenance,
    contract: Contract,
) -> Dict[str, Any]:
    graded = bool(re.search(_GRADED_EFFORT_VERSIONS, ctx.f_val, re.IGNORECASE))

    if graded and ctx.effort not in ("enable", "adaptive"):
        return prov.apply(
            payload,
            contract,
            "graded_effort",
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": _coerce_glm_effort(ctx),
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
    ),
]
