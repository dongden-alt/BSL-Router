"""
Tests for the Kimi K2 family reasoning policy in BSL Router.

Covers:
  - detect_family() correctly identifies all Kimi model ID variants
  - FAMILY_POLICIES["kimi"] uses passback_unsigned replay (not drop)
  - THINKING_CONFIG_MAP["kimi"] maps enable/adaptive/max/high/xhigh → enable_thinking=True
  - get_ui_thinking_options() returns binary off/enabled (no levels)
  - apply_thinking_to_anthropic_payload() actually injects enable_thinking
  - strip_thinking_from_messages() preserves thinking for Kimi (passback_unsigned)
  - Provider profile for "kimi" has supports_thinking=True
"""
import re
from typing import Dict, Any, List

import pytest

from app.compat.reasoning_policy import (
    detect_family,
    get_policy,
    get_thinking_config,
    get_ui_thinking_options,
    apply_thinking_to_anthropic_payload,
    strip_thinking_from_messages,
    FAMILY_POLICIES,
    THINKING_CONFIG_MAP,
    ReasoningPolicy,
)
from app.compat.provider_profiles import PROFILES, ProviderProfile


# ─── detect_family ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model_id,provider_name", [
    ("kimi-k2.7-code", "vsllm-a"),
    ("kimi-k2.6", "vietapi-o"),
    ("kimi-k2.5", "openrouter"),
    ("kimi-2.7", "moonshot"),
    ("kimi-2.6-thinking", "openrouter"),
    ("moonshotai/kimi-k2.6", "openrouter"),
    ("free/kimi-k2.6", "siliconflow"),
])
def test_detect_family_kimi(model_id: str, provider_name: str) -> None:
    """All Kimi model ID variants resolve to the 'kimi' family."""
    assert detect_family(model_id, provider_name) == "kimi"


def test_detect_family_moonshot_provider() -> None:
    """Provider name 'moonshot' triggers kimi family even for generic model IDs."""
    assert detect_family("some-model", "moonshot") == "kimi"


def test_detect_family_k2_dot_in_model_id() -> None:
    """Model IDs containing 'k2.' are Kimi."""
    assert detect_family("k2.7-code", "vsllm-a") == "kimi"


# ─── FAMILY_POLICIES ─────────────────────────────────────────────────────

def test_kimi_family_policy_exists() -> None:
    """FAMILY_POLICIES has a 'kimi' entry."""
    assert "kimi" in FAMILY_POLICIES


def test_kimi_family_policy_uses_passback_unsigned() -> None:
    """Kimi requires reasoning_content replay but isn't first-party Anthropic."""
    policy = FAMILY_POLICIES["kimi"]
    assert policy.replay_policy == "passback_unsigned"


def test_kimi_family_policy_does_not_drop_unsigned() -> None:
    """Unsigned thinking blocks must be preserved for Kimi multi-turn."""
    policy = FAMILY_POLICIES["kimi"]
    assert policy.drop_unsigned is False


def test_kimi_family_policy_injects_thinking_config() -> None:
    """Kimi policy allows thinking config injection (for K2.5/K2.6 enable_thinking)."""
    policy = FAMILY_POLICIES["kimi"]
    assert policy.inject_thinking_config is True


def test_kimi_family_policy_request_fields() -> None:
    """Kimi uses enable_thinking, not reasoning_effort or thinking.type."""
    policy = FAMILY_POLICIES["kimi"]
    assert "enable_thinking" in policy.request_fields


def test_kimi_policy_not_default() -> None:
    """Kimi must NOT fall through to default (which drops thinking)."""
    policy = get_policy("kimi-k2.7-code", "vsllm-a")
    assert policy.name == "kimi"
    assert policy.replay_policy != "drop"


# ─── THINKING_CONFIG_MAP ─────────────────────────────────────────────────

def test_kimi_thinking_config_map_exists() -> None:
    assert "kimi" in THINKING_CONFIG_MAP


def test_kimi_thinking_off_returns_none() -> None:
    """'off' should not inject anything."""
    assert get_thinking_config("kimi-k2.7-code", "off") is None


def test_kimi_thinking_enabled_injects_boolean() -> None:
    """'enabled' maps to enable_thinking=True (boolean, not type/level)."""
    config = get_thinking_config("kimi-k2.6", "enabled")
    assert config == {"enable_thinking": True}


def test_kimi_thinking_adaptive_alias() -> None:
    """'adaptive' is an alias — Kimi is binary, no adaptive mode."""
    config = get_thinking_config("kimi-k2.6", "adaptive")
    assert config == {"enable_thinking": True}


def test_kimi_thinking_max_alias() -> None:
    """'max' maps to True — Kimi has no effort levels."""
    config = get_thinking_config("kimi-k2.7-code", "max")
    assert config == {"enable_thinking": True}


def test_kimi_thinking_high_alias() -> None:
    """'high' maps to True."""
    config = get_thinking_config("kimi-k2.7-code", "high")
    assert config == {"enable_thinking": True}


def test_kimi_thinking_xhigh_alias() -> None:
    """'xhigh' maps to True."""
    config = get_thinking_config("kimi-k2.7-code", "xhigh")
    assert config == {"enable_thinking": True}


def test_kimi_thinking_auto_alias() -> None:
    """'auto' maps to True."""
    config = get_thinking_config("kimi-k2.6", "auto")
    assert config == {"enable_thinking": True}


def test_kimi_no_reasoning_effort_levels() -> None:
    """Kimi must NOT have low/medium/high as separate effort levels (unlike GLM-5.2)."""
    config_map = THINKING_CONFIG_MAP["kimi"]
    # 'high' exists but maps to boolean, not to reasoning_effort
    assert "high" in config_map
    high_config = config_map["high"]
    assert "reasoning_effort" not in high_config
    assert high_config == {"enable_thinking": True}


def test_kimi_no_type_field() -> None:
    """Kimi config must NOT use the Anthropic 'type' field."""
    for key, config in THINKING_CONFIG_MAP["kimi"].items():
        if config is not None:
            assert "type" not in config, f"'{key}' config must not have 'type' field"


# ─── get_ui_thinking_options ─────────────────────────────────────────────

def test_kimi_ui_options_binary() -> None:
    """UI shows only off/enabled — no graduated effort levels."""
    options = get_ui_thinking_options("kimi-k2.7-code")
    assert options == ["off", "enabled"]


def test_kimi_ui_options_no_levels() -> None:
    """Kimi UI must NOT offer low/medium/high (unlike GLM-5.2 or OpenAI)."""
    options = get_ui_thinking_options("kimi-k2.6")
    assert "low" not in options
    assert "medium" not in options
    assert "high" not in options


# ─── apply_thinking_to_anthropic_payload ─────────────────────────────────

def test_apply_thinking_injects_enable_thinking() -> None:
    """enable_thinking must be injected into the outbound payload."""
    payload: Dict[str, Any] = {"model": "kimi-k2.6", "messages": []}
    result = apply_thinking_to_anthropic_payload(
        payload, "kimi-k2.6", "vsllm-a", "enabled"
    )
    assert result["enable_thinking"] is True


def test_apply_thinking_off_does_not_inject() -> None:
    """'off' should not inject enable_thinking."""
    payload: Dict[str, Any] = {"model": "kimi-k2.7-code", "messages": []}
    result = apply_thinking_to_anthropic_payload(
        payload, "kimi-k2.7-code", "vsllm-a", "off"
    )
    assert "enable_thinking" not in result


def test_apply_thinking_does_not_inject_type() -> None:
    """Kimi must NOT get an Anthropic-style 'thinking' field with 'type'."""
    payload: Dict[str, Any] = {"model": "kimi-k2.6", "messages": []}
    result = apply_thinking_to_anthropic_payload(
        payload, "kimi-k2.6", "vsllm-a", "enabled"
    )
    # Should NOT set payload["thinking"] = {"type": ...}
    if "thinking" in result:
        assert "type" not in result["thinking"]


# ─── strip_thinking_from_messages ────────────────────────────────────────

def test_kimi_preserves_thinking_blocks_in_replay() -> None:
    """passback_unsigned must preserve thinking blocks in message history."""
    policy = get_policy("kimi-k2.7-code", "vsllm-a")
    messages: List[Dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me analyze this..."},
                {"type": "text", "text": "Here is my answer."},
            ],
        }
    ]
    result = strip_thinking_from_messages(messages, policy)
    # Thinking block must be preserved (not stripped)
    assert len(result[0]["content"]) == 2
    assert result[0]["content"][0]["type"] == "thinking"


def test_kimi_preserves_unsigned_thinking() -> None:
    """Unsigned thinking blocks are preserved for Kimi (no signature validation)."""
    policy = get_policy("kimi-k2.7-code", "vsllm-a")
    messages: List[Dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Reasoning without signature"},
                {"type": "text", "text": "Answer."},
            ],
        }
    ]
    result = strip_thinking_from_messages(messages, policy)
    # The unsigned thinking block must still be there
    thinking_blocks = [b for b in result[0]["content"] if b.get("type") == "thinking"]
    assert len(thinking_blocks) == 1


# ─── Provider Profile ────────────────────────────────────────────────────

def test_kimi_provider_profile_exists() -> None:
    """Provider profile registry has a 'kimi' entry."""
    assert "kimi" in PROFILES


def test_kimi_provider_profile_supports_thinking() -> None:
    """Kimi profile must declare supports_thinking=True (K2.7 always-on)."""
    profile = PROFILES["kimi"]
    assert profile.supports_thinking is True


def test_kimi_provider_profile_passback_unsigned() -> None:
    """Kimi profile must use passback_unsigned, not drop or provider_native."""
    profile = PROFILES["kimi"]
    assert profile.reasoning_policy == "passback_unsigned"


def test_kimi_provider_profile_has_thinking_request_fields() -> None:
    """Kimi profile must list enable_thinking in thinking_request_fields."""
    profile = PROFILES["kimi"]
    assert "enable_thinking" in profile.thinking_request_fields


# ─── GPT-5 A1 reasoning controls ──────────────────────────────────────────

from app.main import _apply_gpt5_reasoning_controls


def test_gpt5_auto_effort_with_explicit_mode_and_context() -> None:
    payload = _apply_gpt5_reasoning_controls({}, "auto", "pro", "all_turns")
    assert payload == {"reasoning": {"mode": "pro", "context": "all_turns"}}
    assert "reasoning_effort" not in payload
    assert "effort" not in payload["reasoning"]


def test_gpt5_explicit_effort_with_mode_and_context() -> None:
    payload = _apply_gpt5_reasoning_controls({}, "high", "standard", "current_turn")
    assert payload["reasoning_effort"] == "high"
    assert payload["reasoning"] == {
        "effort": "high",
        "mode": "standard",
        "context": "current_turn",
    }


def test_gpt5_all_defaults_do_not_inject_reasoning() -> None:
    assert _apply_gpt5_reasoning_controls({}, "auto", None, None) == {}
