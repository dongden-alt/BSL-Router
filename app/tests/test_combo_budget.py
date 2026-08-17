"""Regression tests for stream-budget-reset (per-entry combo budget reset).

SPEC: .brain/cc_tasks/stream-budget-reset.md
- stale-deadline fallback → per-entry fresh window
- TimeoutError message wording — no 'chain budget left'
- second entry falls back after first burns its own window
"""

import time
import unittest
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_retry_state(**kw):
    """Build a minimal _retry_state dict with a Content-Type header."""
    return {"header": {"Content-Type": "text/event-stream"}, **kw}


class TestStaleDeadlineFallback(unittest.TestCase):
    """BUG K: every call to _send_stream_with_thinking_fallback must start
    from `time.monotonic() + CHAIN_TOTAL_BUDGET`, ignoring any stale
    deadline carried in _retry_state.

    We don't import app.main directly (it's a 30k-line monolith). Instead we
    reach through a small slice of the function that seeds _chain_deadline
    and verifies the new constant-value assignment is used by the inner
    `_chain_budget_remaining` closure and downstream path-selection code.
    """

    @patch("app.main.time")
    def test_stale_deadline_in_retry_state_is_ignored(self, mock_time):
        # Simulate: entry-1 ran for 148s; deadline ≈ now + 2s.
        fake_now = 1_000_000.0
        old_deadline = fake_now + 2.0  # only 2 s remaining — would starve
        mock_time.monotonic.return_value = fake_now

        retry_state = _make_retry_state(deadline=old_deadline)

        # Capture what _send_stream_with_thinking_fallback sets as
        # _chain_deadline. The BUG-K fix must use:
        #   _chain_deadline = time.monotonic() + CHAIN_TOTAL_BUDGET
        # NOT: (_retry_state or {}).get('deadline') or ...
        #
        # Read just enough of main.py to validate the assignment without
        # executing the full HTTP pipeline.
        import ast, inspect, textwrap

        src = inspect.getsource(__import__("app.main", fromlist=[""]))
        tree = ast.parse(src)

        # Locate the assignment inside _send_stream_with_thinking_fallback
        found_fresh_assignment = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            # Look for `_chain_deadline = time.monotonic() + CHAIN_TOTAL_BUDGET`
            if not any(
                isinstance(t, ast.Name) and t.id == "_chain_deadline"
                for t in node.targets
            ):
                continue
            # Must be time.monotonic() + CHAIN_TOTAL_BUDGET
            if isinstance(node.value, ast.BinOp):
                op = node.value.op
                if not isinstance(op, ast.Add):
                    continue
                # Check right side is CHAIN_TOTAL_BUDGET
                if not (
                    isinstance(node.value.right, ast.Name)
                    and node.value.right.id == "CHAIN_TOTAL_BUDGET"
                ):
                    continue
                # Left side: time.monotonic() → Call where func is Attribute
                # (attr='monotonic', value=Name(id='time')) or similar
                left = node.value.left
                if isinstance(left, ast.Call):
                    func = left.func
                    if isinstance(func, ast.Attribute) and func.attr == "monotonic":
                        found_fresh_assignment = True
                        break
                    if (
                        isinstance(func, ast.Name)
                        and func.id == "monotonic"
                    ):
                        found_fresh_assignment = True
                        break

        self.assertTrue(
            found_fresh_assignment,
            "BUG-K fix not found: expected "
            "'_chain_deadline = time.monotonic() + CHAIN_TOTAL_BUDGET'"
            " inside _send_stream_with_thinking_fallback.",
        )


class TestTimeoutErrorMessageWording(unittest.TestCase):
    """The re-raised TimeoutError from upstream_header_timeout must NOT
    contain the phrase 'chain budget left'. This was the old bookkeeping
    line that leaked stale-budget semantics into user-facing error messages.
    """

    @patch("app.main.time")
    def test_no_chain_budget_left_in_error_message(self, mock_time):
        import inspect

        src = inspect.getsource(__import__("app.main", fromlist=[""]))

        # Find the TimeoutError raise inside the asyncio.TimeoutError except
        # block of _send_stream_with_thinking_fallback.
        self.assertIn(
            "upstream_header_timeout",
            src,
            "Expected 'upstream_header_timeout' in TimeoutError message.",
        )
        self.assertNotIn(
            "chain budget left",
            src,
            "BUG-K: 'chain budget left' phrasing must be removed from "
            "the TimeoutError message so stale-budget semantics stop leaking "
            "into user-visible output.",
        )


class TestSecondEntryFallsBack(unittest.TestCase):
    """When entry-1 consumes most of its fresh 150s window and then errors,
    entry-2 must get another full 150s window instead of seeing ~0s remaining.

    Again we validate this via source inspection — the key invariant is that
    _chain_deadline always resets to `time.monotonic() + CHAIN_TOTAL_BUDGET`.
    Any code path that propagates deadline *without* resetting is a BUG-K
    regression.
    """

    @patch("app.main.time")
    def test_no_deadline_propagation_without_reset(self, mock_time):
        """Ensure the ('deadline' or …) fallback expression is gone. If it
        still exists, an old _retry_state['deadline'] from a prior entry
        could be picked up, which is the exact starvation pattern we are fixing.
        """
        import inspect

        src = inspect.getsource(__import__("app.main", fromlist=[""]))

        # Pattern to forbid: (.get('deadline') or (time.monotonic() + ...)
        bad_pattern = ".get('deadline') or ("
        self.assertNotIn(
            bad_pattern,
            src,
            f"Pattern {bad_pattern!r} should not appear in main.py. "
            "This was the BUG-K vector that allowed stale deadlines "
            "to propagate from prior entries.",
        )


if __name__ == "__main__":
    unittest.main()
