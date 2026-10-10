"""Canonical anchor-lane registry -- the SINGLE source of truth for WHICH router
lane a canonical authenticity standard may be harvested from.

USER-SPECIFIED LANE POLICY (2026-10-08, final):

  claude                       -> mintrouter/
  gpt                          -> codex/
  gemini                       -> antigravity/
  moonshot  kimi-k3, k2.7-code -> aibox/kimi/
  deepseek  v4-pro, v4.1-flash -> aibox/ds/
  zhipu     glm-5.2, glm-5.3   -> aibox/zhipu/     (router lists it as aibox/ZHIPU/)
  qwen      3.8-max, max-0902  -> aibox/           (aibox/qwen3.8-max*)

Any OTHER aibox lane of kimi / deepseek / glm (e.g. aibox/kimi-k3 -- Alibaba-served,
aibox/glm-5.3) is a DOUBLE REFERENCE: a second, independently-served copy that is
harvested with a source_tag, never enters the canonical pool, and may corroborate
but never replace a canonical. mintrouter/glm-5.3 is actually glm-5.3-FLASH and is
refused outright.

A standard frozen from the wrong lane silently benchmarks the wrong weights, so the
lane is enforced here, at the harvest guard (app/main.py) AND in the batch driver.

Case matters to the router: it lists ``aibox/ZHIPU/GLM-5.3`` and answers 503 to
``aibox/zhipu/glm-5.3``. Prefix policy is evaluated case-insensitively and
`resolve_routable` maps a typed id onto the router's exact-cased id.
"""
from typing import Iterable, Optional

from app.authenticity.scope import detect_family

# family -> lowercase lane prefixes a CANONICAL harvest may use.
FAMILY_LANE_PREFIXES = {
    "glm":      ("aibox/zhipu/",),
    "kimi":     ("aibox/kimi/",),
    "deepseek": ("aibox/ds/",),
    "qwen":     ("aibox/qwen",),
    "claude":   ("mintrouter/", "antigravity/"),
    "gpt":      ("codex/",),
    "gemini":   ("antigravity/",),
}

# Families whose NON-canonical aibox lanes serve as double references.
DOUBLE_REFERENCE_FAMILIES = ("kimi", "deepseek", "glm")

# Lanes that are refused outright (not even a double), with the reason.
KNOWN_BAD_LANES = {
    "mintrouter/glm-5.3": "actually glm-5.3-FLASH; never a glm-5.3 standard",
    "mintrouter/kimi-k3": "not the kimi lane; use aibox/kimi/kimi-k3",
    "aibox/glm-5.3-prime": "different SKU (prime); not a glm-5.3 canonical or double",
}

# Exact canonical lanes that sit OUTSIDE their family prefix (user directive
# 2026-10-09): deepseek-v4.1-flash is served at aibox/deepseek-v4.1-flash, not
# aibox/ds/. Exact-id match only -- aibox/deepseek-v4-pro stays refused.
CANONICAL_EXACT_LANES = {
    "aibox/deepseek-v4.1-flash": "deepseek-v4.1-flash",
}

# Registered double references: lane -> canonical standard it corroborates.
DOUBLE_REFERENCE_LANES = {
    "aibox/kimi-k3": "kimi-k3",     # Alibaba-served; canonical = aibox/kimi/kimi-k3
    "aibox/glm-5.2": "glm-5.2",     # canonical = aibox/ZHIPU/GLM-5.2
    "aibox/glm-5.3": "glm-5.3",     # canonical = aibox/ZHIPU/GLM-5.3
    # glm-5.3-flash has no official API lane; orcarouter/z-ai/glm-5.3-flash-free is
    # the main reference, this chat2api zai-web2 copy is its independent double.
    "chat2api/zai-web2/glm-5.3-flash-max": "glm-5.3-flash",
}

# canonical standard name -> exact router lane id (case as the router lists it).
CANONICAL_ANCHORS = {
    "claude-fable-5":    "mintrouter/claude-fable-5",
    "claude-fable-5-1":  "mintrouter/claude-fable-5-1",
    "claude-opus-5":     "mintrouter/claude-opus-5",
    "claude-opus-5-5":   "mintrouter/claude-opus-5-5",
    "claude-sonnet-5":   "mintrouter/claude-sonnet-5",
    "claude-sonnet-5-5": "mintrouter/claude-sonnet-5-5",
    "gpt-5.6-sol":       "codex/gpt-5.6-sol",
    "gpt-6-astra":       "codex/gpt-6-astra",
    "gpt-6-luna":        "codex/gpt-6-luna",
    "gpt-6.1-sol":       "codex/gpt-6.1-sol",
    "glm-5.2":           "aibox/ZHIPU/GLM-5.2",
    "glm-5.3":           "aibox/ZHIPU/GLM-5.3",
    "kimi-k3":           "aibox/kimi/kimi-k3",
    "kimi-k2.7-code":    "aibox/kimi/kimi-k2.7-code",
    "qwen3.8-max":       "aibox/qwen3.8-max",
    "qwen3.8-max-0902":  "aibox/qwen3.8-max-0902",
    "deepseek-v4-pro":   "aibox/ds/deepseek-v4-pro",
    "deepseek-v4.1-flash": "aibox/deepseek-v4.1-flash",
    "gemini-3.1-pro":    "antigravity/gemini-pro-agent",   # gemini-3.1-pro-high 502s; pro-agent is the live alias (user 2026-10-09: pro-agent == gemini-3.1-pro-high)
}


def is_double_reference(lane_id: str) -> bool:
    """True for a registered double-reference lane (case-insensitive)."""
    return (lane_id or "").strip().lower() in DOUBLE_REFERENCE_LANES


def _is_double_candidate(lane: str, family: str) -> bool:
    """Any non-canonical aibox lane of kimi/deepseek/glm is a double candidate."""
    return (lane.startswith("aibox/") and family in DOUBLE_REFERENCE_FAMILIES
            and not any(lane.startswith(p) for p in FAMILY_LANE_PREFIXES.get(family, ())))


def lane_violation(lane_id: str) -> Optional[str]:
    """Return a human-readable refusal reason, or None when the lane is a
    legal CANONICAL anchor. Case-insensitive; never raises."""
    lane = (lane_id or "").strip().lower()
    if "/" not in lane:
        return f"'{lane_id}' has no source prefix (expected <source>/<model>)"
    bad = KNOWN_BAD_LANES.get(lane)
    if bad:
        return f"lane '{lane_id}' refused: {bad}"
    if lane in CANONICAL_EXACT_LANES:
        return None
    tail = lane.split("/", 1)[1]
    family = detect_family(tail)
    if not family:
        return f"lane '{lane_id}' refused: model family not recognised"
    prefixes = FAMILY_LANE_PREFIXES.get(family)
    if not prefixes:
        return f"lane '{lane_id}' refused: no canonical lane defined for family '{family}'"
    if not any(lane.startswith(p) for p in prefixes):
        if _is_double_candidate(lane, family):
            return (f"lane '{lane_id}' is a DOUBLE-REFERENCE lane for {family} "
                    f"(source-tagged only); canonical lane is "
                    f"{', '.join(prefixes)}")
        return (f"lane '{lane_id}' refused: {family} standards may only be "
                f"harvested from {', '.join(prefixes)}")
    return None


def resolve_routable(lane_id: str, routable_ids: Iterable[str]) -> Optional[str]:
    """Map a typed lane id onto the router's exact-cased id (the router is
    case-sensitive: ``aibox/zhipu/glm-5.3`` -> 503). None if not routable."""
    want = (lane_id or "").strip().lower()
    for rid in routable_ids:
        if (rid or "").strip().lower() == want:
            return rid
    return None
