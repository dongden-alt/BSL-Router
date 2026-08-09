"""
Middleware.bsl_benchmark_sheet — Canonical family quality scores.

Baked from the approved BSL Matrix v2.0 (bsl_matrix_v1.yaml, 2026-07-16)
which used community-sourced benchmarks (LMSYS Arena, Artificial Analysis,
Terminal-Bench, Harvey LAB-AA, τ³-Banking, HLE, GPQA Diamond).

Each canonical family receives a quality score 0-10 per (category, complexity)
cell, derived from its matrix position:
  - primary    → 10   (best-in-class for this cell)
  - fallback_1 → 7    (strong alternative)
  - fallback_2 → 4    (adequate backup)
  - absent     → 0    (not recommended for this cell)

Tier bonus: deep cells get a +1 reasoning-weight bonus, fast cells get a
-1 penalty, reflecting that reasoning models shine on complex tasks but are
overkill for trivial ones.

This is the READ-ONLY data layer. The auto-select engine (bsl_auto_select.py)
consumes these scores as one factor in the route scoring formula (§4.2).
"""

from typing import Dict, Tuple

from app.middleware.route_registry import normalize_canonical

# ─── Matrix data (from bsl_matrix_v1.yaml v2.0) ─────────────────────────────
# Each cell lists [primary, fallback_1, fallback_2] canonical families.
# 13 categories × 3 complexity tiers = 39 cells.

_MATRIX: Dict[str, Dict[str, Tuple[str, str, str]]] = {
    "general": {
        "fast":      ("glm-5.1",          "deepseek-v4-flash",  "qwen3.6-plus"),
        "standard":  ("glm-5.2",          "kimi-k2.6",          "deepseek-v4-pro"),
        "deep":      ("gpt-5.5",          "claude-opus-4.8",    "gemini-3.1-pro"),
    },
    "business": {
        "fast":      ("glm-5.1",          "deepseek-v4-flash",  "qwen3.6-plus"),
        "standard":  ("gpt-5.5",          "claude-sonnet-5",    "grok-4.5"),
        "deep":      ("gpt-5.6-sol",      "claude-opus-4.8",    "grok-4.5"),
    },
    "law": {
        "fast":      ("claude-sonnet-5",  "glm-5.2",            "qwen3.6-plus"),
        "standard":  ("claude-sonnet-5",  "claude-opus-4.7",    "glm-5.2"),
        "deep":      ("claude-opus-4.8",  "gpt-5.6-sol",        "gemini-3.1-pro"),
    },
    "finance": {
        "fast":      ("deepseek-v4-flash","glm-5.1",            "qwen3.6-plus"),
        "standard":  ("deepseek-v4-pro",  "gpt-5.5",            "glm-5.2"),
        "deep":      ("gpt-5.6-sol",      "claude-opus-4.8",    "deepseek-v4-pro"),
    },
    "geopolitics": {
        "fast":      ("glm-5.1",          "deepseek-v4-flash",  "kimi-k2.6"),
        "standard":  ("gpt-5.5",          "claude-sonnet-5",    "grok-4.5"),
        "deep":      ("claude-opus-4.8",  "gpt-5.6-sol",        "gemini-3.1-pro"),
    },
    "health": {
        "fast":      ("glm-5.1",          "qwen3.6-plus",       "deepseek-v4-flash"),
        "standard":  ("claude-sonnet-5",  "gpt-5.5",            "glm-5.2"),
        "deep":      ("claude-opus-4.8",  "gpt-5.6-sol",        "claude-sonnet-5"),
    },
    "research": {
        "fast":      ("glm-5.1",          "deepseek-v4-flash",  "qwen3.6-plus"),
        "standard":  ("deepseek-v4-pro",  "glm-5.2",            "gemini-3.1-pro"),
        "deep":      ("gpt-5.6-sol",      "deepseek-v4-pro",    "claude-opus-4.8"),
    },
    "science": {
        "fast":      ("glm-5.1",          "deepseek-v4-flash",  "qwen3.6-plus"),
        "standard":  ("deepseek-v4-pro",  "gemini-3.1-pro",     "glm-5.2"),
        "deep":      ("gpt-5.6-sol",      "gemini-3.1-pro",     "deepseek-v4-pro"),
    },
    "creative": {
        "fast":      ("glm-5.2",          "kimi-k2.6",          "gemini-3.5-flash"),
        "standard":  ("claude-sonnet-5",  "glm-5.2",            "gemini-3.1-pro"),
        "deep":      ("claude-opus-4.8",  "gpt-5.6-sol",        "claude-sonnet-5"),
    },
    "technical": {
        "fast":      ("deepseek-v4-flash","glm-5.1",            "kimi-k2.7-code"),
        "standard":  ("glm-5.2",          "kimi-k2.6",          "deepseek-v4-pro"),
        "deep":      ("gpt-5.6-sol",      "deepseek-v4-pro",    "glm-5.2"),
    },
    "education": {
        "fast":      ("glm-5.1",          "kimi-k2.6",          "gemini-3.5-flash"),
        "standard":  ("claude-sonnet-5",  "gpt-5.5",            "glm-5.2"),
        "deep":      ("gpt-5.6-sol",      "claude-opus-4.8",    "gemini-3.1-pro"),
    },
    "lifestyle": {
        "fast":      ("glm-5.1",          "gemini-3.5-flash",   "kimi-k2.6"),
        "standard":  ("glm-5.2",          "gemini-3.1-pro",     "kimi-k2.6"),
        "deep":      ("gpt-5.5",          "claude-sonnet-5",    "gemini-3.1-pro"),
    },
    "philosophy": {
        "fast":      ("glm-5.1",          "deepseek-v4-flash",  "qwen3.6-plus"),
        "standard":  ("claude-sonnet-5",  "gpt-5.5",            "glm-5.2"),
        "deep":      ("claude-opus-4.8",  "gpt-5.6-sol",        "deepseek-v4-pro"),
    },
}

# Position → base quality score
_POSITION_SCORES = {"primary": 10, "fallback_1": 7, "fallback_2": 4}

# Tier adjustment: reasoning models shine on deep; penalize on fast.
_TIER_ADJUSTMENT = {"fast": -1, "standard": 0, "deep": 1}

GLOBAL_LAST_FALLBACK_FAMILY = "glm-5.2"

ALL_CATEGORIES = tuple(_MATRIX.keys())
ALL_TIERS = ("fast", "standard", "deep")


def _build_score_index() -> Dict[str, Dict[str, Dict[str, float]]]:
    """Build category → tier → canonical_family → quality_score."""
    index: Dict[str, Dict[str, Dict[str, float]]] = {}
    for category, tiers in _MATRIX.items():
        index[category] = {}
        for tier, (primary, fb1, fb2) in tiers.items():
            tier_adj = _TIER_ADJUSTMENT.get(tier, 0)
            cell_scores = {}
            for family, position in [(primary, "primary"), (fb1, "fallback_1"), (fb2, "fallback_2")]:
                score = _POSITION_SCORES[position] + tier_adj
                # Key by NORMALIZED form so runtime lookups with canonical_id
                # (dash form, e.g. "gpt-5-5") hit the same entry as the dotted
                # sheet name ("gpt-5.5"). See audit Bug A.
                cell_scores[normalize_canonical(family)] = max(0.0, float(score))
            index[category][tier] = cell_scores
    return index


_SCORE_INDEX = _build_score_index()


def get_family_quality_score(family: str, category: str, complexity_tier: str) -> float:
    """Get the benchmark quality score for a canonical family in a cell.

    Returns 0.0 if the family is not recommended for this cell.
    """
    tier_map = _SCORE_INDEX.get(category, {})
    cell = tier_map.get(complexity_tier, {})
    return cell.get(normalize_canonical(family), 0.0)


def get_cell_families(category: str, complexity_tier: str) -> Tuple[str, str, str]:
    """Get the (primary, fallback_1, fallback_2) canonical families for a cell."""
    tier_map = _MATRIX.get(category, {})
    return tier_map.get(complexity_tier, ("", "", ""))


def get_all_cell_scores(category: str, complexity_tier: str) -> Dict[str, float]:
    """Get the full family→score map for a cell."""
    tier_map = _SCORE_INDEX.get(category, {})
    return dict(tier_map.get(complexity_tier, {}))
