"""Regression tests for the Claude-4 reasoning-contract router.

Operator policy (user, 2026-07-22): ONLY Opus 4.6 antigravity* SKUs use a 32k
thinking BUDGET; every other Opus/Sonnet uses a thinking LEVEL. This test locks
that split so a future edit can't silently coerce the antigravity budget into an
effort level (the earlier _coerce_effort blanket-fix bug) or force enabled+budget
onto a 4.7/4.8 SKU that rejects it with a 400.

Run: .venv\\Scripts\\python -m pytest app/tests/test_claude_modern_thinking.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.main import _claude_modern_thinking, _budget_tokens


# ── _budget_tokens ──────────────────────────────────────────────────────
def test_budget_tokens_detects_budget_forms():
    assert _budget_tokens("32k") == 32768
    assert _budget_tokens("32768") == 32768
    assert _budget_tokens("16k") == 16384
    assert _budget_tokens("64k") == 65536


def test_budget_tokens_rejects_levels_and_tiny_ints():
    for level in ("max", "high", "medium", "low", "xhigh", "adaptive", "enabled", "", None):
        assert _budget_tokens(level) is None
    # A bare tiny integer is NOT a budget.
    assert _budget_tokens("2") is None
    assert _budget_tokens("512") is None


# ── Opus 4.6 antigravity* → budget contract ─────────────────────────────
def test_opus_46_antigravity_uses_budget():
    think, effort, budget = _claude_modern_thinking("vsllm-a/claude-opus-4-6-antigravity", "32k")
    assert think == {"type": "enabled", "budget_tokens": 32768}
    assert effort is None
    assert budget == 32768


def test_opus_46_antigravity_ultra_uses_budget():
    think, effort, budget = _claude_modern_thinking(
        "vsllm-a/claude-opus-4-6-antigravity-ultra", "32k"
    )
    assert think == {"type": "enabled", "budget_tokens": 32768}
    assert budget == 32768
    assert "output_config" not in str(effort)  # no effort level emitted


def test_opus_46_antigravity_dotted_name_also_budget():
    # The dotted antigravity id must match the same 4[.-]6 + antigravity gate.
    think, effort, budget = _claude_modern_thinking(
        "vsllm-a/claude-opus-4.6-antigravity", "32k"
    )
    assert think["type"] == "enabled"
    assert budget == 32768


def test_non_antigravity_opus_46_stays_on_level_even_with_32k():
    # POLICY: ONLY opus-4.6-antigravity* uses the budget. A plain opus-4.6
    # configured with thinking:32k must be coerced to a LEVEL, not a budget.
    think, effort, budget = _claude_modern_thinking("vietapi-a/claude-opus-4.6", "32k")
    assert think == {"type": "adaptive"}
    assert budget is None
    assert effort == "max"  # 32k coerced to a level


# ── Every other Opus/Sonnet → level contract ────────────────────────────
def test_opus_48_uses_effort_level_not_budget():
    think, effort, budget = _claude_modern_thinking("pix4k/claude-opus-4.8-thinking", "max")
    assert think == {"type": "adaptive"}
    assert effort == "max"
    assert budget is None


def test_sonnet_uses_effort_level():
    think, effort, budget = _claude_modern_thinking("pix4k/claude-sonnet-5-thinking", "high")
    assert think == {"type": "adaptive"}
    assert effort == "high"
    assert budget is None


def test_opus_47_never_forced_to_budget_even_if_misconfigured_32k():
    # Safety gate: a mis-set 32k on a 4.7 SKU must NOT produce enabled+budget
    # (which 4.7/4.8 reject with 400). It stays adaptive, and 32k is coerced
    # to a valid level.
    think, effort, budget = _claude_modern_thinking("vsllm-a/claude-opus-4-7", "32k")
    assert think == {"type": "adaptive"}
    assert budget is None
    assert effort == "max"  # 32k coerced to a level, never leaked raw


def test_opus_46_with_level_value_stays_on_level():
    # Non-antigravity opus-4.6 configured with thinking:max (see vsllm-a
    # claude-opus-4-6) must use the level path, not a budget.
    think, effort, budget = _claude_modern_thinking("vsllm-a/claude-opus-4-6", "max")
    assert think == {"type": "adaptive"}
    assert effort == "max"
    assert budget is None
