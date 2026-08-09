"""
Middleware.bsl_agentic_benchmark_sheet — Agentic agent-role model quality scores.

Serves the Agentic/Ultra/Max agent matrices (per-agent 3-slot routes). Unlike
bsl-lite (category x complexity), agents have NO complexity tier in the UI, so
every agent uses the single "standard" tier. The auto-select engine
(bsl_auto_select._get_sheet, target="bsl_agentic") consumes these scores the
same way it does for bsl_lite — one canonical family per slot, no duplicates.

Categories are the granular agent ids configured in the UI:
  scout, vision, fast_coder, power_coder, ultra_coder, refactor,
  frontend_coder, planner, planner_architect, planner_challenger,
  planner_challenger_member, planner_planner, auditor, auditor_reviewer,
  auditor_reviewer_member, auditor_auditor, auditor_auditor_member.

Each family receives a quality score 0-10 per (category, "standard") cell:
  - primary    -> 10   (best-in-class for this cell)
  - fallback_1 -> 7    (strong alternative)
  - fallback_2 -> 4    (adequate backup)
  - absent     -> 0    (not recommended for this cell)

This is the READ-ONLY data layer. The auto-select engine consumes these scores
as one factor in the route scoring formula.
"""

from typing import Dict, Tuple

from app.middleware.route_registry import normalize_canonical

# ─── Matrix data (agent-role aligned) ──────────────────────────────────────
# Each cell lists [primary, fallback_1, fallback_2] canonical families.
# Single "standard" tier: the agentic UI has no complexity tiers, so one cell
# per agent. Families keep the SAME canonical spelling the lite sheet uses so
# the route registry family keys match.
#
# CROSS-ROLE DISTINCTNESS CONTRACT (load-bearing — do not collapse):
# Sub-agents must NOT mirror their parent's cell. Every planner_* role and every
# auditor_* role gets a DISTINCT primary family, because these roles critique
# each other's output:
#   - planner_challenger challenges planner_architect
#   - auditor_reviewer reviews the planner's plan
#   - auditor_auditor audits the coders' code
#   - *_member subagents fan out under their lead
# If a challenger shares a primary with the role it challenges, the challenge is
# self-review by the same model and its adversarial value collapses. The 5
# planner primaries and 5 auditor primaries below are therefore pairwise
# distinct. Enforced by app/tests/test_agentic_sheet_distinctness.py.
#
# The engine (bsl_auto_select) separately guarantees no family repeats WITHIN a
# single cell's 3 slots. That is a different invariant from this one.
_MATRIX: Dict[str, Dict[str, Tuple[str, str, str]]] = {
    "scout": {
        "standard":  ("deepseek-v4-pro",  "glm-5.2",            "kimi-k2.6"),
    },
    "vision": {
        "standard":  ("claude-sonnet-5",  "gpt-5.5",            "gemini-3.1-pro"),
    },
    # ── Planner family: 5 pairwise-distinct primaries ──
    # parent default — deliberately mid-tier; granular roles override it.
    "planner": {
        "standard":  ("gpt-5.5",          "claude-sonnet-5",    "glm-5.2"),
    },
    # Architecture design — strongest reasoning model.
    "planner_architect": {
        "standard":  ("gpt-5.6-sol",      "claude-opus-4.8",    "gpt-5.5"),
    },
    # Challenges the architect. MUST differ from planner_architect or the
    # challenge is the same model reviewing itself.
    "planner_challenger": {
        "standard":  ("claude-opus-4.8",  "gpt-5.6-sol",        "deepseek-v4-pro"),
    },
    # Challenger subagent — different lineage again for genuine fan-out.
    # Fallbacks deliberately differ from scout's chain (which shares this
    # primary) so the two never degrade into the same model sequence.
    "planner_challenger_member": {
        "standard":  ("deepseek-v4-pro",  "kimi-k2.7-code",     "glm-5.1"),
    },
    # Task decomposition — structured output over raw reasoning depth.
    "planner_planner": {
        "standard":  ("claude-sonnet-5",  "gpt-5.5",            "glm-5.2"),
    },

    # ── Auditor family: 5 pairwise-distinct primaries ──
    # parent default — granular roles override it. Fallbacks differ from
    # planner_planner (same primary) so the two chains never fully coincide.
    "auditor": {
        "standard":  ("claude-sonnet-5",  "gemini-3.1-pro",     "kimi-k2.6"),
    },
    # Reviews the PLAN. Differs from every planner_* primary so the reviewer is
    # never the same model that authored the plan.
    "auditor_reviewer": {
        "standard":  ("gemini-3.1-pro",   "claude-opus-4.8",    "gpt-5.5"),
    },
    # Reviewer subagent.
    "auditor_reviewer_member": {
        "standard":  ("glm-5.2",          "kimi-k2.6",          "deepseek-v4-pro"),
    },
    # Audits CODE — strongest code-critique model.
    "auditor_auditor": {
        "standard":  ("claude-opus-4.8",  "gpt-5.6-sol",        "claude-sonnet-5"),
    },
    # Auditor subagent.
    "auditor_auditor_member": {
        "standard":  ("kimi-k2.6",        "deepseek-v4-pro",    "glm-5.2"),
    },
    # ── Coder tiers: distinct primaries matched to tier PURPOSE ──
    # The classifier separates fast/power/ultra by task size, so identical
    # routes would make that distinction meaningless. Each tier escalates:
    # cheap+fast -> balanced -> frontier.
    # Quick fixes / typos — cheapest and fastest; a frontier model is waste here.
    "fast_coder": {
        "standard":  ("glm-5.1",          "kimi-k2.6",          "deepseek-v4-flash"),
    },
    # Standard feature work — balanced workhorse. Fallbacks differ from
    # auditor_reviewer_member, which shares this primary.
    "power_coder": {
        "standard":  ("glm-5.2",          "gpt-5.5",            "kimi-k2.6"),
    },
    # Complex/architectural implementation — strongest general coder.
    # Fallbacks differ from the `planner` parent default (same primary).
    "ultra_coder": {
        "standard":  ("gpt-5.5",          "claude-opus-4.8",    "glm-5.2"),
    },
    # Restructuring — needs large context to hold many files at once, and a
    # code-specialized model. Distinct from power_coder despite similar size.
    "refactor": {
        "standard":  ("kimi-k2.7-code",   "glm-5.2",            "deepseek-v4-pro"),
    },
    # UI/UX — Sonnet leads on frontend. Fallbacks differ from planner_planner,
    # which shares this primary.
    "frontend_coder": {
        "standard":  ("claude-sonnet-5",  "gemini-3.1-pro",     "glm-5.2"),
    },
}

# Position -> base quality score
_POSITION_SCORES = {"primary": 10, "fallback_1": 7, "fallback_2": 4}

# Tier adjustment: agents have no tier in the UI (single "standard" tier),
# so the adjustment is always 0. Kept for shape-compatibility with the
# bsl_lite sheet and the auto-select engine.
_TIER_ADJUSTMENT = {"standard": 0}

GLOBAL_LAST_FALLBACK_FAMILY = "glm-5.2"

ALL_CATEGORIES = tuple(_MATRIX.keys())
ALL_TIERS = ("standard",)


def _build_score_index() -> Dict[str, Dict[str, Dict[str, float]]]:
    """Build category -> tier -> canonical_family -> quality_score."""
    index: Dict[str, Dict[str, Dict[str, float]]] = {}
    for category, tiers in _MATRIX.items():
        index[category] = {}
        for tier, (primary, fb1, fb2) in tiers.items():
            tier_adj = _TIER_ADJUSTMENT.get(tier, 0)
            cell_scores = {}
            for family, position in [(primary, "primary"), (fb1, "fallback_1"), (fb2, "fallback_2")]:
                score = _POSITION_SCORES[position] + tier_adj
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
    """Get the full family->score map for a cell."""
    tier_map = _SCORE_INDEX.get(category, {})
    return dict(tier_map.get(complexity_tier, {}))
