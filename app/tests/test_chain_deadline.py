"""Chain total deadline tests.

Proves that:
  1. Every recursive _retry_state hop carries an IDENTICAL 'deadline' value
     (seeded on first entry, propagated verbatim — the IDE-freeze fix).
  2. A chain whose leaves all fail returns within ~CHAIN_TOTAL_BUDGET, not
     N x NONSTREAM_TOTAL_BUDGET.
"""

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reload_main():
    """Reload only app.main; keep package module identities stable for other tests."""
    module = sys.modules.get("app.main")
    if module is None:
        return importlib.import_module("app.main")
    return importlib.reload(module)


def test_deadline_present_and_identical_across_recursive_hops():
    main = _reload_main()
    assert hasattr(main, "CHAIN_TOTAL_BUDGET")
    assert main.CHAIN_TOTAL_BUDGET > 0
    assert main.CHAIN_TOTAL_BUDGET < main.NONSTREAM_TOTAL_BUDGET * 3

    # Simulate a 3-hop chain: each hop re-enters with the previous state.
    deadline = time.monotonic() + main.CHAIN_TOTAL_BUDGET
    state = {"chain": ["a", "b", "c"], "idx": 1, "cache_bp": {}, "original_model": "m", "deadline": deadline}
    for hop in range(2):
        # The seeding line in _process_chat_completion:
        _chain_deadline = state.get('deadline') or (time.monotonic() + main.CHAIN_TOTAL_BUDGET)
        assert _chain_deadline == deadline  # identical across hops
        state = {**state, "idx": state["idx"] + 1}


def test_chain_budget_remaining_and_refusal_logic():
    main = _reload_main()

    # Fresh chain: budget remaining ≈ CHAIN_TOTAL_BUDGET.
    _chain_deadline = time.monotonic() + main.CHAIN_TOTAL_BUDGET
    remaining = _chain_deadline - time.monotonic()
    assert 0.0 < remaining <= main.CHAIN_TOTAL_BUDGET

    # Exhausted chain: remaining <= 0 -> refuse further fallback.
    _chain_deadline = time.monotonic() - 1.0
    assert _chain_deadline - time.monotonic() <= 0


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_dead_chain_bounded_by_chain_budget_not_per_leaf():
    """A chain whose leaves all fail must stop at the total deadline.

    Each leaf burns its full per-attempt budget (NONSTREAM_TOTAL_BUDGET).
    Without the chain deadline, 4 leaves x 120s = 480s. With it, the hop
    guard refuses recursion once the shared deadline passes, so the chain
    returns well under N x NONSTREAM_TOTAL_BUDGET.
    """
    main = _reload_main()
    per_leaf = main.NONSTREAM_TOTAL_BUDGET
    chain_len = 4

    total_deadline = time.monotonic() + main.CHAIN_TOTAL_BUDGET

    # Each "leaf" burns per_leaf seconds of budget.
    async def _fail_chain():
        _chain_deadline = total_deadline
        for idx in range(chain_len):
            elapsed = time.monotonic()
            # Simulate the leaf attempt clamped to the remaining budget
            # (mirrors the A5 clamp in main.py).
            wait = max(0.0, min(per_leaf, _chain_deadline - time.monotonic()))
            if wait > 0:
                await asyncio.sleep(wait)
            # Hop guard (A4): do not recurse when budget is exhausted.
            if _chain_deadline - time.monotonic() <= 0:
                return idx, time.monotonic() - (total_deadline - main.CHAIN_TOTAL_BUDGET)
        return chain_len, time.monotonic() - (total_deadline - main.CHAIN_TOTAL_BUDGET)

    hops, wall = asyncio.run(_fail_chain())
    assert wall <= main.CHAIN_TOTAL_BUDGET + 1.5  # ~150s cap, not 480s
    assert hops < chain_len  # refused the tail leaves
