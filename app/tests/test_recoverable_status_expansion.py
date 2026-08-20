"""Regression: _RECOVERABLE must include client/validation 4xx so chains never stop early."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main


# Every status that must trigger combo/chain advance (never terminal stop).
EXPECTED_RECOVERABLE = {
    400, 401, 403, 404, 405, 408, 409, 413, 422, 429,
    500, 502, 503, 504, 524, 525, 526,
}


def test_recoverable_includes_expanded_4xx_and_5xx():
    assert EXPECTED_RECOVERABLE.issubset(main._RECOVERABLE)
    assert main._RECOVERABLE == EXPECTED_RECOVERABLE


def test_previously_terminal_4xx_are_now_recoverable():
    """400/401/405/409/413/422 used to terminate the chain; they must advance now."""
    for code in (400, 401, 405, 409, 413, 422):
        assert code in main._RECOVERABLE, f"{code} must be recoverable"


def test_error_reports_module_attr_exists():
    """BUG1: /api/observability/artifacts reads obs.error_reports at module level."""
    import app.observability as obs

    assert hasattr(obs, "error_reports")
    assert isinstance(obs.error_reports, list)


def test_get_artifacts_defensive_getattr():
    """get_artifacts must not AttributeError if error_reports is missing."""
    import inspect

    src = inspect.getsource(main.get_artifacts)
    assert 'getattr(obs, "error_reports", [])' in src


def test_antigravity_slots_include_gemini_36():
    for slot in (
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
    ):
        assert slot in main.ANTIGRAVITY_INTEGRATION_SLOTS
    # Order: 3.6 entries prepend before 3.5
    slots = main.ANTIGRAVITY_INTEGRATION_SLOTS
    assert slots.index("gemini-3.6-flash-high") < slots.index("gemini-3.5-flash-medium")
