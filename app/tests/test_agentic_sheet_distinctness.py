"""Guard the cross-role distinctness contract in the agentic benchmark sheet.

Regression test for the "all planner agents share the same model" class of bug.

Two INDEPENDENT invariants are covered here:

1. WITHIN-cell distinctness — no family appears twice in one agent's 3 slots.
   (Also enforced by the auto-select engine, but asserted here at the data
   layer so a bad sheet fails loudly instead of relying on downstream dedupe.)

2. CROSS-role distinctness — every planner_* role has a distinct primary, and
   every auditor_* role has a distinct primary. This is the load-bearing one:
   these roles critique each other, so if a challenger shares a primary with
   the role it challenges, the "challenge" is the same model reviewing its own
   output and its adversarial value collapses.

History: the sheet originally set sub-agent cells to mirror their parent's
cell, which produced only 2 distinct primaries across 5 planner roles —
planner_architect and planner_challenger were identical.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.middleware.bsl_agentic_benchmark_sheet import _MATRIX  # noqa: E402
from app.middleware.route_registry import normalize_canonical  # noqa: E402

PLANNER_ROLES = [
    "planner",
    "planner_architect",
    "planner_challenger",
    "planner_challenger_member",
    "planner_planner",
]

AUDITOR_ROLES = [
    "auditor",
    "auditor_reviewer",
    "auditor_reviewer_member",
    "auditor_auditor",
    "auditor_auditor_member",
]

# The classifier separates these by task size/kind. If they resolve to the same
# model the distinction is decorative — a "fast" fix would burn the same model
# as a full architectural change.
CODER_ROLES = [
    "fast_coder",
    "power_coder",
    "ultra_coder",
    "refactor",
    "frontend_coder",
]


def _primary(role: str) -> str:
    return normalize_canonical(_MATRIX[role]["standard"][0])


def test_no_duplicate_family_within_a_cell() -> None:
    """No agent may use the same canonical family twice across its 3 slots."""
    for role, tiers in _MATRIX.items():
        for tier, slots in tiers.items():
            families = [normalize_canonical(f) for f in slots if f]
            assert len(families) == len(set(families)), (
                f"{role}/{tier} repeats a canonical family: {slots}"
            )


def test_planner_roles_have_distinct_primaries() -> None:
    """All 5 planner roles must resolve to different primary families."""
    primaries = {role: _primary(role) for role in PLANNER_ROLES}
    unique = set(primaries.values())
    assert len(unique) == len(PLANNER_ROLES), (
        f"planner roles share primaries: {primaries}"
    )


def test_auditor_roles_have_distinct_primaries() -> None:
    """All 5 auditor roles must resolve to different primary families."""
    primaries = {role: _primary(role) for role in AUDITOR_ROLES}
    unique = set(primaries.values())
    assert len(unique) == len(AUDITOR_ROLES), (
        f"auditor roles share primaries: {primaries}"
    )


def test_coder_tiers_have_distinct_primaries() -> None:
    """fast/power/ultra/refactor/frontend must not collapse to one model.

    Regression: fast_coder, power_coder and refactor were once byte-identical
    (glm-5.2, kimi-k2.6, deepseek-v4-pro), making the classifier's fast-vs-power
    distinction change nothing at runtime.
    """
    primaries = {role: _primary(role) for role in CODER_ROLES}
    unique = set(primaries.values())
    assert len(unique) == len(CODER_ROLES), (
        f"coder tiers share primaries: {primaries}"
    )


def test_no_two_roles_share_an_identical_full_triplet() -> None:
    """No two agents anywhere may have the SAME 3-slot chain.

    Distinct primaries alone are not enough — two roles could differ only in
    primary while sharing both fallbacks, which still collapses under failover.
    """
    seen: dict[tuple, str] = {}
    for role, tiers in _MATRIX.items():
        triplet = tuple(normalize_canonical(f) for f in tiers["standard"])
        if triplet in seen:
            raise AssertionError(
                f"{role!r} and {seen[triplet]!r} have identical chains: {triplet}"
            )
        seen[triplet] = role


def test_challenger_differs_from_architect() -> None:
    """The adversarial pair must never be the same model.

    This is the specific collapse that motivated the test: a challenger running
    on the same model as the architect is self-review, not a challenge.
    """
    assert _primary("planner_challenger") != _primary("planner_architect"), (
        "planner_challenger must not share a primary with planner_architect"
    )


def test_reviewer_differs_from_all_planners() -> None:
    """The plan reviewer must not be any planner's primary model.

    auditor_reviewer reviews the planner's output; sharing a model with the
    author defeats the review.
    """
    reviewer = _primary("auditor_reviewer")
    planner_primaries = {_primary(r) for r in PLANNER_ROLES}
    assert reviewer not in planner_primaries, (
        f"auditor_reviewer primary {reviewer!r} collides with a planner primary"
    )


def test_every_role_has_a_full_three_slot_chain() -> None:
    """Every cell must define all 3 slots — an empty fallback_2 was the original
    reason Auto-Select only ever filled Primary and Fallback 1."""
    for role, tiers in _MATRIX.items():
        for tier, slots in tiers.items():
            assert len(slots) == 3, f"{role}/{tier} has {len(slots)} slots"
            for idx, fam in enumerate(slots):
                assert fam and fam.strip(), (
                    f"{role}/{tier} slot {idx} is empty — Auto-Select will skip it"
                )
