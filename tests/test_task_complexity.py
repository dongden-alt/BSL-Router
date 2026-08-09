"""Tests for P4 Task Complexity Router middleware."""
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import ChatCompletionRequest, Message
from app.middleware.task_complexity import (
    estimate_request_complexity,
    apply_task_complexity_routing,
    COMPLEXITY_TRIVIAL,
    COMPLEXITY_STANDARD,
    COMPLEXITY_DEEP,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _req(messages, max_tokens=None, model="test-model"):
    return ChatCompletionRequest(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
    )


def _user(text):
    return Message(role="user", content=text)


# ─── Config fixtures ──────────────────────────────────────────────────────────

DISABLED_CFG = {"tools": {"task_complexity_router": False}}

ENABLED_CFG = {"tools": {
    "task_complexity_router": True,
    "task_complexity_min_tokens": 1024,
    "task_complexity_max_tokens": 65536,
    "task_complexity_trivial_tokens": 2048,
    "task_complexity_standard_tokens": 8192,
    "task_complexity_deep_tokens": 16384,
    "task_complexity_allow_lowering": False,
}}

ENABLED_LOWERING_CFG = {"tools": {
    "task_complexity_router": True,
    "task_complexity_min_tokens": 1024,
    "task_complexity_max_tokens": 65536,
    "task_complexity_trivial_tokens": 2048,
    "task_complexity_standard_tokens": 8192,
    "task_complexity_deep_tokens": 16384,
    "task_complexity_allow_lowering": True,
}}


# ─── Classification tests ─────────────────────────────────────────────────────

def test_trivial_classification():
    """Greeting / tiny single-turn → trivial."""
    req = _req([_user("hi")])
    d = estimate_request_complexity(req)
    assert d.level == COMPLEXITY_TRIVIAL, f"Expected trivial, got {d.level} (score={d.score})"


def test_small_factual_classification():
    """Short factual request → standard (funnel: no trivial match, no deep signals)."""
    req = _req([_user("What is 2+2? Just the number.")])
    d = estimate_request_complexity(req)
    assert d.level == COMPLEXITY_STANDARD, \
        f"Expected standard, got {d.level} (score={d.score}) [funnel: no signals fire -> standard default]"


def test_standard_classification():
    """A multi-message coding request with moderate context → standard.
    Uses enough tokens + code markers + conversation depth to reliably
    reach score 4-6 (standard) without tipping into complex."""
    code = (
        "def process_data(items):\n"
        "    results = []\n"
        "    for item in items:\n"
        "        if item.is_valid():\n"
        "            results.append(item.transform())\n"
        "    return results\n"
    )
    msgs = [
        _user("I need help with a data processing module."),
        Message(role="assistant", content="Sure, what do you need?"),
        _user(
            f"Write a Python function in app/utils/processor.py:\n"
            f"```python\n{code}\n```\n"
            f"That processes items and returns valid results. "
            f"Make sure to handle edge cases."
        ),
    ]
    req = _req(msgs)
    d = estimate_request_complexity(req)
    assert d.level == COMPLEXITY_STANDARD, \
        f"Expected standard, got {d.level} (score={d.score}) [funnel: code+markers alone don't reach deep without D3]"

def test_deep_classification():
    """Multi-file/debug/refactor request → deep."""
    text = (
        "I need to debug and refactor the entire authentication module. "
        "The issue is in app/auth/login.py at line 42 where the token "
        "validation fails. Please review and fix the traceback:\n"
        "Traceback (most recent call last):\n"
        "  File 'app/auth/login.py', line 42, in validate_token\n"
        "    raise Exception('Invalid token')\n"
        "Also audit app/auth/middleware.py and app/auth/models.py "
        "and integrate the fix end-to-end with regression tests."
    )
    req = _req([_user(text)])
    d = estimate_request_complexity(req)
    assert d.level == COMPLEXITY_DEEP, \
        f"Expected deep, got {d.level} (score={d.score})"


# ─── Behavioural tests ────────────────────────────────────────────────────────

def test_disabled_noop():
    """When feature flag is off, max_tokens must not change."""
    req = _req([_user("hi")], max_tokens=1000)
    result = apply_task_complexity_routing(req, DISABLED_CFG)
    assert result.max_tokens == 1000


def test_missing_max_tokens_lifts_to_target():
    """When max_tokens is None, it should be set to output ceiling (flat)."""
    req = _req([_user("hi")], max_tokens=None)
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.max_tokens is not None
    assert result.max_tokens == 65536  # flat output ceiling, not tier-dependent


def test_low_max_tokens_lifts():
    """When max_tokens is below output ceiling, it should be lifted."""
    req = _req([_user("hi")], max_tokens=100)
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.max_tokens == 65536


def test_high_max_tokens_not_lowered_by_default():
    """When max_tokens is above output ceiling and allow_lowering is False, keep existing."""
    req = _req([_user("hi")], max_tokens=131072)
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.max_tokens == 131072


def test_high_max_tokens_lowered_when_allowed():
    """When allow_lowering is True and max_tokens is above output ceiling, lower to target."""
    req = _req([_user("hi")], max_tokens=131072)
    result = apply_task_complexity_routing(req, ENABLED_LOWERING_CFG)
    assert result.max_tokens == 65536  # lowered to output ceiling


def test_clamp_to_max():
    """Final max_tokens must not exceed configured cap."""
    cfg = {"tools": {
        "task_complexity_router": True,
        "task_complexity_min_tokens": 1024,
        "task_complexity_max_tokens": 4096,  # very low cap
        "task_complexity_deep_tokens": 16384,
        "task_complexity_allow_lowering": True,
    }}
    text = (
        "Debug and refactor and audit and test and implement and integrate "
        "and migrate the entire architecture end-to-end with regression. "
        "Fix app/main.py at line 100, review app/models.py, trace the "
        "Traceback (most recent call last) in app/utils.py line 50."
    )
    req = _req([_user(text)], max_tokens=None)
    result = apply_task_complexity_routing(req, cfg)
    assert result.max_tokens <= 4096


def test_clamp_to_min():
    """Final max_tokens must not go below configured min."""
    cfg = {"tools": {
        "task_complexity_router": True,
        "task_complexity_min_tokens": 8192,
        "task_complexity_max_tokens": 65536,
        "task_complexity_trivial_tokens": 1024,
        "task_complexity_allow_lowering": True,
    }}
    req = _req([_user("hi")], max_tokens=None)
    result = apply_task_complexity_routing(req, cfg)
    assert result.max_tokens >= 8192


def test_messages_preserved():
    """Middleware must not modify messages."""
    msgs = [_user("hello world")]
    req = _req(msgs, max_tokens=1000)
    original_content = req.messages[0].content
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.messages[0].content == original_content
    assert len(result.messages) == len(msgs)


def test_tool_context_detected():
    """Messages with tool_calls should add score."""
    from app.models import ToolCall, ToolCallFunction
    msg = Message(
        role="assistant",
        content="Let me check that.",
        tool_calls=[ToolCall(
            id="call_1",
            type="function",
            function=ToolCallFunction(name="check", arguments='{"q": "test"}'),
        )],
    )
    req = _req([_user("check this"), msg, Message(role="tool", content="result", tool_call_id="call_1")])
    d = estimate_request_complexity(req)
    assert "tool context present" in d.reasons


# --- P0 Integration: fail-open + budget floor interaction ---

def test_fail_open_on_exception():
    """If the classifier throws, the request must be returned unchanged."""
    req = _req([_user("hi")], max_tokens=500)
    bad_cfg = {"tools": {
        "task_complexity_router": True,
        "task_complexity_min_tokens": "not_an_int",
    }}
    result = apply_task_complexity_routing(req, bad_cfg)
    assert result.max_tokens == 500, \
        f"Fail-open should preserve original max_tokens, got {result.max_tokens}"


def test_router_budget_respects_target_not_floor():
    """When router sets a budget, the main.py budget floor should NOT
    override it back to 65536."""
    req = _req([_user("hi")], max_tokens=None)
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.max_tokens == 65536  # flat output ceiling

    # Simulate the main.py budget floor logic with P0 fix
    _task_complexity_controls_budget = True
    _model_max_output_tokens = 0
    _mt_floor = 65536
    _client_mt = result.max_tokens

    if _task_complexity_controls_budget:
        if _model_max_output_tokens > 0 and _client_mt > _model_max_output_tokens:
            final = _model_max_output_tokens
        else:
            final = _client_mt
    elif _model_max_output_tokens > 0:
        final = min(max(_client_mt, _mt_floor), _model_max_output_tokens)
    else:
        final = max(_client_mt, _mt_floor)

    assert final == 65536, f"Router budget should be respected. Got {final}"


def test_router_budget_with_model_cap():
    """When router sets a budget above the model hard cap, cap wins."""
    req = _req([_user("hi")], max_tokens=None)
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.max_tokens == 65536  # flat output ceiling

    _task_complexity_controls_budget = True
    _model_max_output_tokens = 1024
    _client_mt = result.max_tokens

    if _task_complexity_controls_budget:
        if _model_max_output_tokens > 0 and _client_mt > _model_max_output_tokens:
            final = _model_max_output_tokens
        else:
            final = _client_mt
    else:
        final = _client_mt

    assert final == 1024, f"Model hard cap should clamp. Got {final}"


def test_router_kept_budget_not_inflated_by_floor():
    """When router is enabled and intentionally keeps an existing budget,
    the legacy 65536 floor must not inflate it."""
    req = _req([_user("hi")], max_tokens=131072)
    result = apply_task_complexity_routing(req, ENABLED_CFG)
    assert result.max_tokens == 131072

    _task_complexity_controls_budget = True
    _model_max_output_tokens = 0
    _mt_floor = 65536
    _client_mt = result.max_tokens

    if _task_complexity_controls_budget:
        if _model_max_output_tokens > 0 and _client_mt > _model_max_output_tokens:
            final = _model_max_output_tokens
        else:
            final = _client_mt
    elif _model_max_output_tokens > 0:
        final = min(max(_client_mt, _mt_floor), _model_max_output_tokens)
    else:
        final = max(_client_mt, _mt_floor)

    assert final == 131072, f"Kept router budget should not be floor-inflated. Got {final}"


def test_no_floor_override_when_router_disabled():
    """When router is disabled, legacy floor behavior applies."""
    req = _req([_user("hi")], max_tokens=100)
    result = apply_task_complexity_routing(req, DISABLED_CFG)
    assert result.max_tokens == 100

    _task_complexity_controls_budget = False
    _model_max_output_tokens = 0
    _mt_floor = 65536
    _client_mt = result.max_tokens

    if _task_complexity_controls_budget:
        final = _client_mt
    elif _model_max_output_tokens > 0:
        final = min(max(_client_mt, _mt_floor), _model_max_output_tokens)
    else:
        final = max(_client_mt, _mt_floor)

    assert final == 65536, f"Legacy floor should apply. Got {final}"


# ─── Labeled regression fixture tests ───────────────────────────────────────


def test_labeled_fixture_cases():
    """Run all labeled CASES through estimate_request_complexity."""
    from tests.fixtures.complexity_cases import CASES
    for text, expected, note in CASES:
        req = _req([_user(text)])
        d = estimate_request_complexity(req)
        assert d.level == expected, (
            f"CASE: {text[:60]!r}...\n"
            f"  Expected {expected}, got {d.level} (fv={d.feature_vector})\n"
            f"  Note: {note}"
        )


def test_continuation_inheritance_deep_root():
    """make it longer after a deep root -> deep (continuation floor)."""
    from tests.fixtures.complexity_cases import CONTINUATION_CASES
    continuation_text, root_text, expected, note = (
        "make it longer",
        "Write a 5000-word thesis on quantum computing with detailed explanations of entanglement, superposition, and quantum gates",
        "deep",
        "continuation after deep root -> inherits deep (floor)",
    )
    msgs = [
        _user(root_text),
        Message(role="assistant", content="Here is the thesis."),
        _user(continuation_text),
    ]
    req = _req(msgs)
    d = estimate_request_complexity(req)
    assert d.level == COMPLEXITY_DEEP, (
        f"Expected deep (inherited from root), got {d.level} fv={d.feature_vector}"
    )


def test_continuation_no_overpromote_standard_root():
    """make it longer after a standard root -> standard (no over-promotion)."""
    msgs = [
        _user("Explain how JWT works"),
        Message(role="assistant", content="JWT is..."),
        _user("make it longer"),
    ]
    req = _req(msgs)
    d = estimate_request_complexity(req)
    assert d.level == COMPLEXITY_STANDARD, (
        f"Expected standard (inherited from standard root), got {d.level} fv={d.feature_vector}"
    )


def test_feature_vector_present_on_decision():
    """ComplexityDecision should carry a feature_vector after funnel."""
    req = _req([_user("design a billing system: 6 requirements, style Y, tests")])
    d = estimate_request_complexity(req)
    assert isinstance(d.feature_vector, dict)
    # Must contain at least some of the D-keys
    for key in ("D1_scope", "D3_density"):
        assert key in d.feature_vector, f"Missing D-key {key} in fv={d.feature_vector}"


def test_budget_max_tokens_is_65536():
    """ComplexityDecision.budget_max_tokens must be 65536 regardless of tier."""
    for text, _, _ in [
        ("hi", COMPLEXITY_TRIVIAL, "trivial"),
        ("Explain JWT", COMPLEXITY_STANDARD, "standard"),
        ("debug and refactor the auth module, integrate end-to-end", COMPLEXITY_DEEP, "deep"),
    ]:
        req = _req([_user(text)])
        d = estimate_request_complexity(req)
        assert d.budget_max_tokens == 65536, (
            f"Expected budget_max_tokens=65536 for '{text}', got {d.budget_max_tokens}"
        )
