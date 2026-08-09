"""Regression tests for the effort:"32k" leak fix.

`output_config.effort` and `reasoning.effort` accept reasoning effort LEVELS
(low/medium/high/max, plus xhigh on some channels) — NOT a token budget. A
budget-style config `thinking` value like "32k" previously leaked straight
through as an invalid effort, producing a malformed body that Anthropic-
compatible origins (pix4k opus) rejected. Live outbound capture confirmed 6
records shipped effort="32k". `_coerce_effort` maps budget forms to a level
while passing real effort words through unchanged.

Run: .venv\\Scripts\\python -m pytest app/tests/test_effort_coercion.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.main import _coerce_effort


def test_budget_suffix_forms_map_to_a_level():
    # "k" suffix budget -> level by size (<=16k medium, else max).
    assert _coerce_effort("32k") == "max"
    assert _coerce_effort("64k") == "max"
    assert _coerce_effort("128k") == "max"
    assert _coerce_effort("16k") == "medium"
    assert _coerce_effort("8k") == "medium"


def test_raw_token_counts_map_to_a_level():
    assert _coerce_effort("32768") == "max"
    assert _coerce_effort("16384") == "medium"
    assert _coerce_effort("2048") == "medium"


def test_real_effort_words_pass_through_unchanged():
    # The fix must NEVER alter a working effort value.
    for word in ("low", "medium", "high", "max", "xhigh", "adaptive", "enabled"):
        assert _coerce_effort(word) == word


def test_case_and_whitespace_normalized():
    assert _coerce_effort("  MAX ") == "max"
    assert _coerce_effort("32K") == "max"


def test_empty_and_none_are_safe():
    assert _coerce_effort("") == ""
    assert _coerce_effort(None) == ""


def test_no_invalid_budget_effort_survives():
    # The exact leaked value from the outbound capture must be coerced away.
    assert _coerce_effort("32k") in ("low", "medium", "high", "max")
