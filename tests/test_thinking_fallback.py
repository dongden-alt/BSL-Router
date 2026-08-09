"""Unit tests for reseller channel-roulette resilience.

Covers:
  1. thinking_fallback pure helpers (detection / has / strip).
  2. error_prevention out_tokens ban-immunity for token-producing requests.

Run: .venv\\Scripts\\python -m pytest tests/test_thinking_fallback.py -q
Or standalone: .venv\\Scripts\\python tests/test_thinking_fallback.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.middleware.thinking_fallback import (
    is_thinking_param_rejection,
    payload_has_thinking,
    strip_thinking,
    THINKING_PAYLOAD_KEYS,
)
from app.error_prevention import ErrorPreventionManager, record_outcome


# ── is_thinking_param_rejection ────────────────────────────────────────────
def test_detects_exact_iamhc_400_body():
    body = '{"error":{"message":"Validation: Unsupported parameter(s): `thinking`","type":"Bad Request","param":"","code":400}}'
    assert is_thinking_param_rejection(400, body) is True


def test_detects_reasoning_named_rejection():
    assert is_thinking_param_rejection(400, "the reasoning field is not supported by this model") is True


def test_ignores_non_400_status():
    body = "Unsupported parameter(s): `thinking`"
    assert is_thinking_param_rejection(500, body) is False
    assert is_thinking_param_rejection(200, body) is False


def test_ignores_unrelated_400():
    assert is_thinking_param_rejection(400, "invalid api key") is False
    assert is_thinking_param_rejection(400, "context length exceeded") is False


def test_empty_body_is_false():
    assert is_thinking_param_rejection(400, "") is False
    assert is_thinking_param_rejection(400, None) is False


# ── payload_has_thinking ────────────────────────────────────────────────────
def test_has_thinking_detects_each_key():
    for key in THINKING_PAYLOAD_KEYS:
        assert payload_has_thinking({"model": "x", key: "anything"}) is True, key


def test_has_thinking_false_when_absent():
    assert payload_has_thinking({"model": "x", "messages": []}) is False


def test_has_thinking_bad_input():
    assert payload_has_thinking(None) is False
    assert payload_has_thinking("nope") is False


# ── strip_thinking ──────────────────────────────────────────────────────────
def test_strip_removes_all_thinking_keys_without_mutating_input():
    payload = {
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "output_config": {"effort": "high"},
        "max_tokens": 65536,
    }
    stripped = strip_thinking(payload)
    # Original untouched
    assert "thinking" in payload and "reasoning_effort" in payload
    # Stripped has none of the thinking keys
    for key in THINKING_PAYLOAD_KEYS:
        assert key not in stripped
    # Non-thinking fields preserved
    assert stripped["model"] == "glm-5.2"
    assert stripped["messages"] == payload["messages"]
    assert stripped["max_tokens"] == 65536


def test_strip_noop_when_no_thinking():
    payload = {"model": "x", "messages": []}
    assert strip_thinking(payload) == payload


def test_strip_bad_input_passthrough():
    assert strip_thinking(None) is None


# ── error_prevention: token-producing requests must not ban ─────────────────
def _mgr_config():
    return {
        "error_prevention": {"enabled": True, "consecutive_threshold": 3},
        "error_prevention_state": {},
    }


def test_out_tokens_prevents_ban_on_tail_timeout():
    cfg = _mgr_config()
    # Simulate a slow stream that delivered tokens but attached a tail timeout error.
    for _ in range(5):
        record_outcome(cfg, "vsllm-a", "glm-5.2", 200, "read timeout", out_tokens=14)
    mgr = ErrorPreventionManager(cfg)
    banned, _, _ = mgr.is_banned("vsllm-a", "glm-5.2")
    assert banned is False


def test_zero_out_tokens_error_still_bans():
    cfg = _mgr_config()
    # Genuine failures (no tokens produced) must still escalate to a softban.
    for _ in range(3):
        record_outcome(cfg, "iamhc-2", "DeepSeek-V4-Pro", 404, "not found", out_tokens=0)
    mgr = ErrorPreventionManager(cfg)
    banned, ban_type, _ = mgr.is_banned("iamhc-2", "DeepSeek-V4-Pro")
    assert banned is True
    assert ban_type == "softban"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa
                failures += 1
                print(f"ERROR {name}: {e}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
