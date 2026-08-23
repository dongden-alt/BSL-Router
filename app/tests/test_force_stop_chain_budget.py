"""
Regression tests for the 2026-08-23 force-stop fix (deadline-stall fallback veto).

BUG: CHAIN_TOTAL_BUDGET (150s) < max(STREAM_DEADLINE_LADDER) (600s) inverted
the fallback invariant. A deadline-stall fire happens at >=660s into an entry
(observed: 668s, 672s), by which time the per-entry budget was ALWAYS
exhausted, so _deadline_stall_fire refused the combo advance ("refusing
further fallback") and degraded a recoverable stall into a terminal 504
(_DeadlineRetryNeeded -> gemini_egress_stream_guarded terminal frame).
The client saw this as a force-stop ("network issue connecting to the
server" / "model output must contain either output text or tool calls").

FIX: CHAIN_TOTAL_BUDGET must cover the entry worst case:
  HEADER_WAIT_TIMEOUT (300s) + max ladder rung (600s) + 60s margin = 960s.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _main():
    return importlib.import_module("app.main")


def test_chain_budget_covers_max_ladder_rung():
    """Budget must exceed the largest ladder rung or deadline fires can never advance."""
    main = _main()
    max_rung = max(main.STREAM_DEADLINE_LADDER)
    assert main.CHAIN_TOTAL_BUDGET > max_rung, (
        f"CHAIN_TOTAL_BUDGET={main.CHAIN_TOTAL_BUDGET}s <= max ladder rung "
        f"{max_rung}s: every deadline-stall fire would find the budget "
        "exhausted and the combo advance would be vetoed (force-stop bug)"
    )


def test_chain_budget_covers_header_wait_plus_max_rung():
    """Budget must cover HEADER_WAIT_TIMEOUT + max rung + advance margin."""
    main = _main()
    worst_case = main.HEADER_WAIT_TIMEOUT + max(main.STREAM_DEADLINE_LADDER) + 60.0
    assert main.CHAIN_TOTAL_BUDGET >= worst_case, (
        f"CHAIN_TOTAL_BUDGET={main.CHAIN_TOTAL_BUDGET}s < worst case "
        f"{worst_case}s (header wait {main.HEADER_WAIT_TIMEOUT}s + max rung "
        f"{max(main.STREAM_DEADLINE_LADDER)}s + 60s margin): a stall after a "
        "slow header would find the budget exhausted (force-stop bug)"
    )


def test_observed_deadline_fires_have_budget_remaining():
    """Observed production fires (668s/672s into an entry) must land inside the budget."""
    main = _main()
    for observed_elapsed in (668.0, 672.0):
        assert main.CHAIN_TOTAL_BUDGET > observed_elapsed, (
            f"CHAIN_TOTAL_BUDGET={main.CHAIN_TOTAL_BUDGET}s does not cover an "
            f"observed deadline fire at {observed_elapsed}s (force-stop bug)"
        )


def test_deadline_fire_can_take_fallback_arithmetic():
    """Simulate _deadline_stall_fire's budget check with the production numbers.

    At fire time the entry has consumed ~668s. The fallback branch requires
    _chain_budget_remaining() > 0, i.e. budget must exceed consumed time.
    """
    main = _main()
    consumed = 668.0  # observed: 668.6s / 672.5s end-to-end
    deadline_remaining = main.CHAIN_TOTAL_BUDGET - consumed
    assert deadline_remaining > 0, (
        "with the fixed budget a deadline fire at 668s leaves "
        f"{deadline_remaining:.0f}s — the combo advance must be permitted"
    )
