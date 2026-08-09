"""
BSL Router Agent Compatibility Layer — Reasoning / Thinking Policy Engine

Phase 3: No more global thinking patch. Each provider/model family gets its
own reasoning policy that determines how thinking blocks are handled in
requests and replayed across turns.

Policy enum:
  drop                         — strip all thinking/reasoning fields
  passback_unsigned            — pass thinking back without signature validation
  passback_signed_only         — require valid Anthropic signature (first-party only)
  normalize_to_reasoning_content — convert to DeepSeek-style reasoning_content
  openai_responses_reasoning_items — use OpenAI Responses reasoning item format
  provider_native              — let the provider handle it natively
"""
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import json


@dataclass
class ReasoningPolicy:
    """Policy for how thinking/reasoning is handled for a model family."""
    name: str
    request_fields: List[str]       # Fields to inject in outbound request
    replay_policy: str              # How to handle thinking in multi-turn replay
    drop_unsigned: bool             # Whether to drop unsigned thinking blocks
    inject_thinking_config: bool    # Whether to inject thinking config if absent


# ─────────────────────────────────────────────────────────────────────
# Family-level policies
# ─────────────────────────────────────────────────────────────────────

FAMILY_POLICIES: Dict[str, ReasoningPolicy] = {
    "anthropic": ReasoningPolicy(
        name="anthropic",
        request_fields=["thinking"],
        replay_policy="passback_signed_only",
        drop_unsigned=True,
        inject_thinking_config=False,
    ),
    "glm-5.1": ReasoningPolicy(
        name="glm-5.1",
        request_fields=["thinking"],
        replay_policy="provider_native",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "glm-5.2": ReasoningPolicy(
        name="glm-5.2",
        request_fields=["thinking", "reasoning_effort"],
        replay_policy="provider_native",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "deepseek": ReasoningPolicy(
        name="deepseek",
        request_fields=["reasoning", "reasoning_content"],
        replay_policy="normalize_to_reasoning_content",
        drop_unsigned=False,
        inject_thinking_config=False,
    ),
    # Kimi K2 family:
    #   K2.7 Code / K2 Thinking — always-on reasoning, no parameter needed.
    #   K2.5 / K2.6           — toggleable via enable_thinking boolean.
    # All variants return reasoning_content and REQUIRE it to be passed back
    # in multi-turn history.  We use passback_unsigned (not provider_native)
    # because Kimi is NOT first-party Anthropic and does not produce valid
    # Anthropic thinking signatures — but it still demands the reasoning
    # trace in the conversation history on subsequent turns.
    "kimi": ReasoningPolicy(
        name="kimi",
        request_fields=["enable_thinking"],  # only injected for K2.5/K2.6
        replay_policy="passback_unsigned",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "gemini": ReasoningPolicy(
        name="gemini",
        request_fields=["thinking_config"],
        replay_policy="provider_native",
        drop_unsigned=False,
        inject_thinking_config=True,
    ),
    "openai": ReasoningPolicy(
        name="openai",
        request_fields=["reasoning_effort"],
        replay_policy="openai_responses_reasoning_items",
        drop_unsigned=False,
        inject_thinking_config=False,
    ),
    "default": ReasoningPolicy(
        name="default",
        request_fields=[],
        replay_policy="drop",
        drop_unsigned=True,
        inject_thinking_config=False,
    ),
}

# ─────────────────────────────────────────────────────────────────────
# Thinking config value maps per model
# ─────────────────────────────────────────────────────────────────────

# GLM-5.1 thinking UI options: off / enabled / adaptive
# GLM-5.2 thinking UI options: off / low / medium / max
THINKING_CONFIG_MAP: Dict[str, Dict[str, Any]] = {
    "glm-5.1": {
        "off": None,  # Don't inject
        "enabled": {"type": "enabled"},
        "adaptive": {"type": "adaptive"},
    },
    "glm-5.2": {
        "off": None,
        "low": {"type": "enabled", "reasoning_effort": "low"},
        "medium": {"type": "enabled", "reasoning_effort": "medium"},
        "max": {"type": "enabled", "reasoning_effort": "max"},
    },
    "deepseek": {
        "off": None,
        "enabled": {"reasoning": True},
        "adaptive": {"reasoning": True},  # DeepSeek doesn't have adaptive, treat as enabled
    },
    # Kimi K2 family thinking config:
    #   K2.7 Code      — always-on, inject enable_thinking=True as a no-op hint.
    #   K2.5 / K2.6    — toggleable, enable_thinking boolean controls reasoning.
    # There are NO reasoning-effort levels (low/medium/high) for Kimi.
    # The 'enable' and 'adaptive' BSL Router vocabularies both map to
    # enable_thinking=True; 'max'/'high'/'xhigh' also map to True since
    # Kimi does not support graduated effort — thinking is binary.
    "kimi": {
        "off": None,                                     # Don't inject (K2.7 ignores)
        "enabled": {"enable_thinking": True},            # K2.5/K2.6 toggle on
        "adaptive": {"enable_thinking": True},           # alias — Kimi is binary
        "auto": {"enable_thinking": True},               # alias — maps to on
        "max": {"enable_thinking": True},                # alias — Kimi has no effort levels
        "high": {"enable_thinking": True},               # alias
        "xhigh": {"enable_thinking": True},              # alias
    },
}


def detect_family(model_id: str, provider_name: str) -> str:
    """Detect model family from model ID and provider name."""
    model_lower = model_id.lower()

    if "claude" in model_lower or provider_name in ("anthropic", "claude"):
        return "anthropic"
    if "glm-5.2" in model_lower or "glm4" in model_lower:
        return "glm-5.2"
    if "glm-5.1" in model_lower or "glm" in model_lower:
        return "glm-5.1"
    # Kimi / Moonshot — check before generic OpenAI catch-all.
    # Matches: kimi-k2.7-code, kimi-k2.6, kimi-k2.5, kimi-2.6-thinking,
    #          moonshotai/kimi-k2.6, free/kimi-k2.6, etc.
    if "kimi" in model_lower or "k2." in model_lower or "moonshot" in provider_name.lower():
        return "kimi"
    if "deepseek" in model_lower:
        return "deepseek"
    if "gemini" in model_lower or provider_name == "gemini":
        return "gemini"
    if model_lower.startswith(("gpt-", "o1", "o3", "o4")) or provider_name == "openai":
        return "openai"

    return "default"


def get_policy(model_id: str, provider_name: str) -> ReasoningPolicy:
    """Get the reasoning policy for a model/provider combination."""
    family = detect_family(model_id, provider_name)
    return FAMILY_POLICIES.get(family, FAMILY_POLICIES["default"])


def get_thinking_config(model_id: str, thinking_setting: str) -> Optional[Dict[str, Any]]:
    """
    Resolve a thinking setting (e.g. 'high', 'adaptive', 'off') to the
    provider-specific config object.

    Returns None if thinking should not be injected.
    """
    family = detect_family(model_id, "")
    config_map = THINKING_CONFIG_MAP.get(family, {})

    if thinking_setting.lower() not in config_map:
        # Unknown setting — don't inject
        return None

    return config_map[thinking_setting.lower()]


def apply_thinking_to_anthropic_payload(
    payload: Dict[str, Any],
    model_id: str,
    provider_name: str,
    thinking_setting: str = "off",
) -> Dict[str, Any]:
    """
    Inject thinking config into an Anthropic-format outbound payload.

    For GLM models, this sets the `thinking` field on the request.
    For first-party Anthropic, this only injects if the client already
    sent thinking config (we don't fabricate thinking for Anthropic).
    """
    policy = get_policy(model_id, provider_name)

    if not policy.inject_thinking_config:
        # Don't inject — let the client's thinking config pass through if present
        return payload

    config = get_thinking_config(model_id, thinking_setting)
    if config is None:
        # Thinking is off — don't inject
        return payload

    # Inject thinking config — config IS the thinking object (e.g. {"type": "enabled"})
    if "type" in config:
        payload["thinking"] = {"type": config["type"]}
    if "reasoning_effort" in config:
        payload["reasoning_effort"] = config["reasoning_effort"]
    if "reasoning" in config:
        payload["reasoning"] = config["reasoning"]
    if "enable_thinking" in config:
        payload["enable_thinking"] = config["enable_thinking"]

    return payload


def strip_thinking_from_messages(messages: List[Dict[str, Any]], policy: ReasoningPolicy) -> List[Dict[str, Any]]:
    """
    Strip or preserve thinking blocks in message history based on policy.

    For 'drop' and 'passback_signed_only' (with unsigned blocks), removes
    thinking content blocks from assistant messages.
    For 'provider_native' and 'passback_unsigned', preserves them.
    For 'normalize_to_reasoning_content', converts thinking to reasoning_content.
    """
    if policy.replay_policy in ("provider_native", "passback_unsigned", "openai_responses_reasoning_items"):
        return messages

    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue

        new_content = []
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            block_type = block.get("type", "")

            if block_type == "thinking":
                if policy.replay_policy == "drop":
                    continue  # Strip
                elif policy.replay_policy == "passback_signed_only":
                    # Only keep if it has a valid signature
                    if block.get("signature"):
                        new_content.append(block)
                    # else drop unsigned
                elif policy.replay_policy == "normalize_to_reasoning_content":
                    # Convert to reasoning_content field (DeepSeek style)
                    # The thinking text becomes reasoning_content on the message
                    thinking_text = block.get("thinking", "") or block.get("text", "")
                    if thinking_text and "reasoning_content" not in msg:
                        msg["reasoning_content"] = thinking_text
                    continue  # Don't keep the thinking block
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        msg = {**msg, "content": new_content}
        cleaned.append(msg)

    return cleaned


def get_ui_thinking_options(model_id: str) -> List[str]:
    """
    Return the available thinking UI options for a model.

    GLM-5.1: off / enabled / adaptive
    GLM-5.2: off / low / medium / max
    DeepSeek: off / enabled
    Others: off
    """
    family = detect_family(model_id, "")

    if family == "glm-5.1":
        return ["off", "enabled", "adaptive"]
    if family == "glm-5.2":
        return ["off", "low", "medium", "max"]
    if family == "deepseek":
        return ["off", "enabled"]
    if family == "kimi":
        # K3 is a REAL reasoning_effort model: low/high/max (NO medium).
        # K2 is binary enable_thinking. detect_family returns "kimi" for
        # both, so disambiguate on the model id here.
        if "k3" in model_id.lower():
            return ["low", "high", "max"]
        return ["off", "enabled"]
    if family == "gemini":
        return ["off", "enabled"]
    if family == "openai":
        return ["off", "low", "medium", "high", "xhigh", "max"]

    return ["off"]
