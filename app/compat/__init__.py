"""
BSL Router Agent Compatibility Layer

Phase 2-7 modules for protocol-aware agent routing.
"""
from app.compat.provider_profiles import get_profile, is_anthropic_compatible, ProviderProfile
from app.compat.reasoning_policy import (
    get_policy, get_thinking_config, apply_thinking_to_anthropic_payload,
    strip_thinking_from_messages, get_ui_thinking_options, detect_family,
)
from app.compat.stream_normalizer import StreamNormalizer
from app.compat.tool_ledger import ToolLedger

__all__ = [
    "get_profile",
    "is_anthropic_compatible",
    "ProviderProfile",
    "get_policy",
    "get_thinking_config",
    "apply_thinking_to_anthropic_payload",
    "strip_thinking_from_messages",
    "get_ui_thinking_options",
    "detect_family",
    "StreamNormalizer",
    "ToolLedger",
]
