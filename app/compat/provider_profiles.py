"""
BSL Router Agent Compatibility Layer — Provider Profile Registry

Phase 2: Profiles drive routing behavior instead of hardcoded provider-name branches.

Each profile declares:
  - upstream_protocol: what format the provider expects
  - endpoint_path: what URL path to append
  - headers: required/forbidden headers
  - tools: how tools are formatted
  - reasoning: thinking policy reference
  - stream: SSE dialect and prefill policy
"""
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field


@dataclass
class ProviderProfile:
    """Declarative profile for a provider's protocol dialect."""
    id: str
    upstream_protocol: str  # anthropic_messages | openai_chat | gemini | responses
    endpoint_path: str      # /messages | /chat/completions | /v1beta/models/.../generateContent
    base_url_kind: str      # anthropic_compatible | openai_compatible | gemini | custom

    # Header policy
    requires_anthropic_version: bool = False
    allows_anthropic_beta: bool = False  # Only first-party Anthropic

    # Tool policy
    tools_request_format: str = "openai_tools"    # openai_tools | anthropic_tools
    tools_response_format: str = "openai_tool_calls"  # openai_tool_calls | anthropic_tool_use
    strict_tool_pairing: bool = True

    # Reasoning
    reasoning_policy: str = "drop"  # see ReasoningPolicyEngine
    supports_thinking: bool = False
    thinking_request_fields: List[str] = field(default_factory=list)

    # Stream
    stream_dialect: str = "openai_sse"  # openai_sse | anthropic_sse
    allow_mock_prefill: bool = True      # Only safe for non-agent OpenAI clients

    # Compatibility label
    agent_mode_label: str = "untested"  # native-agent | translated-agent | agent-lite | chat-edit-only | untested

    # Special routing
    custom_path_logic: Optional[str] = None  # e.g. "kiro", "gemini_native"


# ─────────────────────────────────────────────────────────────────────
# Profile Registry
# ─────────────────────────────────────────────────────────────────────

PROFILES: Dict[str, ProviderProfile] = {
    "anthropic": ProviderProfile(
        id="anthropic",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=True,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="passback_signed_only",
        supports_thinking=True,
        thinking_request_fields=["thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="native-agent",
    ),

    "glm": ProviderProfile(
        id="glm",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="provider_native",
        supports_thinking=True,
        thinking_request_fields=["thinking", "reasoning_effort"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),

    "kimi": ProviderProfile(
        id="kimi",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        # Kimi is NOT first-party Anthropic — cannot validate thinking
        # signatures.  But K2.7 Code always emits reasoning_content and
        # REQUIRES it in multi-turn replay, so we must preserve unsigned.
        reasoning_policy="passback_unsigned",
        supports_thinking=True,   # K2.7 always-on; K2.5/K2.6 toggleable
        thinking_request_fields=["enable_thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),

    "minimax": ProviderProfile(
        id="minimax",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="provider_native",
        supports_thinking=True,
        thinking_request_fields=["thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),

    "minimax-cn": ProviderProfile(
        id="minimax-cn",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="provider_native",
        supports_thinking=True,
        thinking_request_fields=["thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),

    "deepseek": ProviderProfile(
        id="deepseek",
        upstream_protocol="openai_chat",
        endpoint_path="/chat/completions",
        base_url_kind="openai_compatible",
        requires_anthropic_version=False,
        allows_anthropic_beta=False,
        tools_request_format="openai_tools",
        tools_response_format="openai_tool_calls",
        reasoning_policy="normalize_to_reasoning_content",
        supports_thinking=True,
        thinking_request_fields=["reasoning", "reasoning_content"],
        stream_dialect="openai_sse",
        allow_mock_prefill=True,
        agent_mode_label="translated-agent",
    ),

    "openai": ProviderProfile(
        id="openai",
        upstream_protocol="openai_chat",
        endpoint_path="/chat/completions",
        base_url_kind="openai_compatible",
        requires_anthropic_version=False,
        allows_anthropic_beta=False,
        tools_request_format="openai_tools",
        tools_response_format="openai_tool_calls",
        reasoning_policy="openai_responses_reasoning_items",
        supports_thinking=True,
        thinking_request_fields=["reasoning_effort"],
        stream_dialect="openai_sse",
        allow_mock_prefill=True,
        agent_mode_label="native-agent",
    ),

    "vietapi": ProviderProfile(
        id="vietapi",
        upstream_protocol="openai_chat",
        endpoint_path="/chat/completions",
        base_url_kind="openai_compatible",
        requires_anthropic_version=False,
        allows_anthropic_beta=False,
        tools_request_format="openai_tools",
        tools_response_format="openai_tool_calls",
        reasoning_policy="drop",
        supports_thinking=False,
        stream_dialect="openai_sse",
        allow_mock_prefill=True,
        agent_mode_label="translated-agent",
    ),

    "opencode_go": ProviderProfile(
        id="opencode_go",
        upstream_protocol="openai_chat",
        endpoint_path="/chat/completions",
        base_url_kind="openai_compatible",
        requires_anthropic_version=False,
        allows_anthropic_beta=False,
        tools_request_format="openai_tools",
        tools_response_format="openai_tool_calls",
        reasoning_policy="drop",
        supports_thinking=False,
        stream_dialect="openai_sse",
        allow_mock_prefill=True,
        agent_mode_label="agent-lite",
    ),

    "gemini": ProviderProfile(
        id="gemini",
        upstream_protocol="gemini",
        endpoint_path="",  # Custom path logic
        base_url_kind="gemini",
        requires_anthropic_version=False,
        allows_anthropic_beta=False,
        tools_request_format="openai_tools",
        tools_response_format="openai_tool_calls",
        reasoning_policy="provider_native",
        supports_thinking=True,
        thinking_request_fields=["thinking_config"],
        stream_dialect="openai_sse",
        allow_mock_prefill=True,
        agent_mode_label="translated-agent",
        custom_path_logic="gemini_native",
    ),

    "kiro": ProviderProfile(
        id="kiro",
        upstream_protocol="openai_chat",
        endpoint_path="/generateAssistantResponse",
        base_url_kind="custom",
        requires_anthropic_version=False,
        allows_anthropic_beta=False,
        tools_request_format="openai_tools",
        tools_response_format="openai_tool_calls",
        reasoning_policy="drop",
        supports_thinking=False,
        stream_dialect="openai_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
        custom_path_logic="kiro",
    ),

    "mimo-free": ProviderProfile(
        id="mimo-free",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="provider_native",
        supports_thinking=True,
        thinking_request_fields=["thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),

    "alicode-intl": ProviderProfile(
        id="alicode-intl",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="provider_native",
        supports_thinking=True,
        thinking_request_fields=["thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),

    "claude": ProviderProfile(
        id="claude",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=True,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="passback_signed_only",
        supports_thinking=True,
        thinking_request_fields=["thinking"],
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="native-agent",
    ),

    "kilocode": ProviderProfile(
        id="kilocode",
        upstream_protocol="anthropic_messages",
        endpoint_path="/messages",
        base_url_kind="anthropic_compatible",
        requires_anthropic_version=True,
        allows_anthropic_beta=False,
        tools_request_format="anthropic_tools",
        tools_response_format="anthropic_tool_use",
        reasoning_policy="provider_native",
        supports_thinking=False,
        stream_dialect="anthropic_sse",
        allow_mock_prefill=False,
        agent_mode_label="translated-agent",
    ),
}


# Providers that use Anthropic Messages protocol even though they're not first-party
ANTHROPIC_COMPATIBLE_IDS = {
    p.id for p in PROFILES.values() if p.upstream_protocol == "anthropic_messages"
}


def get_profile(provider_name: str, provider_config: Optional[Dict] = None) -> ProviderProfile:
    """
    Resolve a ProviderProfile for the given provider.

    Checks provider_config for a 'format' override first, then falls back
    to the registry, then defaults to OpenAI-compatible.
    """
    # Check config-level format override
    if provider_config:
        fmt = provider_config.get("format", "").lower()
        if fmt == "anthropic" and provider_name not in PROFILES:
            # Config says anthropic but no explicit profile — build a synthetic one
            return ProviderProfile(
                id=provider_name,
                upstream_protocol="anthropic_messages",
                endpoint_path="/messages",
                base_url_kind="anthropic_compatible",
                requires_anthropic_version=True,
                allows_anthropic_beta=False,
                tools_request_format="anthropic_tools",
                tools_response_format="anthropic_tool_use",
                reasoning_policy="provider_native",
                supports_thinking=True,
                thinking_request_fields=["thinking"],
                stream_dialect="anthropic_sse",
                allow_mock_prefill=False,
                agent_mode_label="translated-agent",
            )

    # Direct registry lookup
    if provider_name in PROFILES:
        return PROFILES[provider_name]

    # Default: OpenAI-compatible
    return ProviderProfile(
        id=provider_name,
        upstream_protocol="openai_chat",
        endpoint_path="/chat/completions",
        base_url_kind="openai_compatible",
    )


def is_anthropic_compatible(profile: ProviderProfile) -> bool:
    """Check if this profile speaks Anthropic Messages protocol."""
    return profile.upstream_protocol == "anthropic_messages"
