"""
BSL Router — Full Agent Compatibility Layer Test Suite

Tests all phases:
  Phase 1: Tool preservation + Anthropic normalization
  Phase 2: Provider profile registry
  Phase 3: Reasoning policy engine
  Phase 4: Stream normalizer
  Phase 6: Responses API
  Claude Code alias resolution
"""
import json
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.normalizer import UniversalNormalizer
from app.models import ChatCompletionRequest
from app.compat import get_profile, is_anthropic_compatible, ToolLedger
from app.compat.reasoning_policy import (
    detect_family, get_policy, get_thinking_config,
    apply_thinking_to_anthropic_payload, get_ui_thinking_options,
)
from app.compat.stream_normalizer import StreamNormalizer
from app.compat.responses_api import ResponsesConverter


def section(title):
    print(f"\n{'-' * 50}")
    print(f"  {title}")
    print(f"{'-' * 50}")


# ═══════════════════════════════════════════════════════════════
# PHASE 1: Tool preservation (from existing test)
# ═══════════════════════════════════════════════════════════════

def test_anthropic_ingress_preserves_tools():
    anthropic_request = {
        "model": "glm-4.6",
        "max_tokens": 4096,
        "system": "You are a helpful coding assistant.",
        "messages": [{"role": "user", "content": "Read the file app/main.py"}],
        "tools": [{
            "name": "Read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}
        }],
        "tool_choice": {"type": "auto"},
        "stream": True
    }
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(anthropic_request)
    assert "tools" in openai_body, "FAIL: tools dropped!"
    assert len(openai_body["tools"]) == 1
    assert openai_body["tool_choice"] == "auto"
    print("  [PASS] Anthropic ingress preserves tools + tool_choice")


def test_tool_roundtrip():
    anthropic_request = {
        "model": "glm-4.6",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me calculate."},
                {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "echo 4"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "4"}
            ]}
        ],
        "stream": False
    }
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(anthropic_request)
    internal = ChatCompletionRequest.model_validate(openai_body)
    out = UniversalNormalizer.normalize_to_anthropic(internal)

    # Find tool_use in outbound
    tool_use_found = False
    tool_result_found = False
    for msg in out["messages"]:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use" and block["id"] == "toolu_01":
                        tool_use_found = True
                    if block.get("type") == "tool_result" and block["tool_use_id"] == "toolu_01":
                        tool_result_found = True

    assert tool_use_found, "FAIL: tool_use missing in outbound"
    assert tool_result_found, "FAIL: tool_result missing in outbound"
    print("  [PASS] tool_use/tool_result full round-trip")


# ═══════════════════════════════════════════════════════════════
# PHASE 2: Provider Profile Registry
# ═══════════════════════════════════════════════════════════════

def test_profile_registry_anthropic():
    p = get_profile("anthropic")
    assert p.upstream_protocol == "anthropic_messages"
    assert p.allows_anthropic_beta == True
    assert p.agent_mode_label == "native-agent"
    assert is_anthropic_compatible(p) == True
    print("  [PASS] Anthropic profile correct")


def test_profile_registry_glm():
    p = get_profile("glm")
    assert p.upstream_protocol == "anthropic_messages"
    assert p.allows_anthropic_beta == False
    assert p.requires_anthropic_version == True
    assert p.agent_mode_label == "translated-agent"
    assert is_anthropic_compatible(p) == True
    print("  [PASS] GLM profile correct (anthropic-compatible, no beta)")


def test_profile_registry_deepseek():
    p = get_profile("deepseek")
    assert p.upstream_protocol == "openai_chat"
    assert p.allows_anthropic_beta == False
    assert is_anthropic_compatible(p) == False
    print("  [PASS] DeepSeek profile correct (openai-compatible)")


def test_profile_registry_unknown_provider():
    p = get_profile("some_random_provider")
    assert p.upstream_protocol == "openai_chat"
    assert is_anthropic_compatible(p) == False
    print("  [PASS] Unknown provider defaults to OpenAI-compatible")


def test_profile_registry_config_override():
    """Config says format=anthropic for a custom provider."""
    cfg = {"format": "anthropic", "name": "my_custom_claude"}
    p = get_profile("my_custom_claude", cfg)
    assert p.upstream_protocol == "anthropic_messages"
    assert is_anthropic_compatible(p) == True
    print("  [PASS] Config format override works")


# ═══════════════════════════════════════════════════════════════
# PHASE 3: Reasoning Policy Engine
# ═══════════════════════════════════════════════════════════════

def test_detect_family():
    assert detect_family("claude-sonnet-4-20250514", "") == "anthropic"
    assert detect_family("glm-4.6", "") == "glm-5.1"  # GLM family
    assert detect_family("deepseek-v4", "") == "deepseek"
    assert detect_family("gemini-2.5-pro", "") == "gemini"
    assert detect_family("gpt-4o", "") == "openai"
    print("  [PASS] Family detection correct")


def test_glm51_thinking_options():
    opts = get_ui_thinking_options("glm-4.6")
    assert "off" in opts
    assert "enabled" in opts
    assert "adaptive" in opts
    print(f"  [PASS] GLM-5.1 thinking options: {opts}")


def test_glm52_thinking_options():
    opts = get_ui_thinking_options("glm-5.2")
    assert opts == ["off", "low", "medium", "max"]
    print(f"  [PASS] GLM-5.2 thinking options: {opts}")


def test_thinking_injection_glm():
    payload = {"model": "glm-4.6", "messages": [], "max_tokens": 100}
    result = apply_thinking_to_anthropic_payload(payload, "glm-4.6", "glm", "adaptive")
    assert "thinking" in result, f"FAIL: thinking not injected. Payload: {result}"
    assert result["thinking"] == {"type": "adaptive"}
    print("  [PASS] GLM thinking injection works")


def test_thinking_injection_off():
    payload = {"model": "glm-4.6", "messages": [], "max_tokens": 100}
    result = apply_thinking_to_anthropic_payload(payload, "glm-4.6", "glm", "off")
    assert "thinking" not in result, "FAIL: thinking should not be injected when off"
    print("  [PASS] Thinking off = no injection")


def test_thinking_injection_anthropic_no_inject():
    """First-party Anthropic should NOT get thinking injected by BSL."""
    payload = {"model": "claude-sonnet-4", "messages": [], "max_tokens": 100}
    result = apply_thinking_to_anthropic_payload(payload, "claude-sonnet-4", "anthropic", "high")
    assert "thinking" not in result, "FAIL: should not inject thinking for first-party Anthropic"
    print("  [PASS] Anthropic first-party: no thinking injection")


# ═══════════════════════════════════════════════════════════════
# PHASE 4: Stream Normalizer (basic structure tests)
# ═══════════════════════════════════════════════════════════════

def test_stream_normalizer_needs_conversion():
    s = StreamNormalizer("openai_sse", "anthropic_sse")
    assert s.needs_conversion() == True

    s2 = StreamNormalizer("openai_sse", "openai_sse")
    assert s2.needs_conversion() == False
    print("  [PASS] Stream normalizer dialect detection works")


async def _run_stream_conversion_test():
    """Convert a simple OpenAI stream to Anthropic SSE."""
    # Simulate OpenAI SSE chunks
    openai_chunks = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_stream():
        for chunk in openai_chunks:
            yield chunk

    normalizer = StreamNormalizer("openai_sse", "anthropic_sse")
    events = []
    async for chunk in normalizer.convert_openai_to_anthropic(mock_stream()):
        text = chunk.decode("utf-8")
        events.append(text)

    # Should have message_start, content_block_start, 2x content_block_delta, content_block_stop, message_delta, message_stop
    assert any("message_start" in e for e in events), "FAIL: no message_start"
    assert any("content_block_start" in e for e in events), "FAIL: no content_block_start"
    assert any("text_delta" in e for e in events), "FAIL: no text_delta"
    assert any("message_stop" in e for e in events), "FAIL: no message_stop"

    # Verify text content
    full_text = ""
    for e in events:
        if "text_delta" in e:
            data_lines = [l for l in e.split("\n") if l.startswith("data: ")]
            for dl in data_lines:
                data = json.loads(dl[6:])
                if data.get("delta", {}).get("type") == "text_delta":
                    full_text += data["delta"]["text"]
    assert full_text == "Hello world", f"FAIL: text mismatch: '{full_text}'"

    print("  [PASS] OpenAI-to-Anthropic stream conversion produces correct events")


def test_stream_conversion():
    asyncio.run(_run_stream_conversion_test())


# ═══════════════════════════════════════════════════════════════
# TOOL LEDGER
# ═══════════════════════════════════════════════════════════════

def test_tool_ledger_clean():
    ledger = ToolLedger()
    messages = [
        {"role": "user", "content": "Read the file"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "main.py"}}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}
        ]}
    ]
    ledger.scan_inbound_messages(messages)
    issues = ledger.validate()
    assert len(issues) == 0, f"FAIL: expected no issues, got {issues}"
    summary = ledger.summary()
    assert summary["resolved"] == 1
    print("  [PASS] Tool ledger: clean conversation has no issues")


def test_tool_ledger_orphan():
    ledger = ToolLedger()
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "nonexistent", "content": "result"}
        ]}
    ]
    ledger.scan_inbound_messages(messages)
    issues = ledger.validate()
    assert len(issues) == 1, f"FAIL: expected 1 orphan issue, got {issues}"
    assert "orphaned" in issues[0].lower()
    print("  [PASS] Tool ledger: orphaned tool_result detected")


# ═══════════════════════════════════════════════════════════════
# CLAUDE CODE ALIAS RESOLUTION
# ═══════════════════════════════════════════════════════════════

def test_alias_resolution_exact():
    """Exact match alias resolution."""
    import fnmatch

    aliases = {
        "claude-sonnet-4-20250514": "combo_sonnet",
        "claude-3-5-haiku-20241022": "combo_haiku",
    }
    raw_model = "claude-sonnet-4-20250514"

    resolved = None
    if raw_model in aliases:
        resolved = aliases[raw_model]
    else:
        for pattern, alias in aliases.items():
            if fnmatch.fnmatch(raw_model.lower(), pattern.lower()):
                resolved = alias
                break

    assert resolved == "combo_sonnet", f"FAIL: expected combo_sonnet, got {resolved}"
    print("  [PASS] Alias resolution: exact match works")


def test_alias_resolution_wildcard():
    """Wildcard pattern alias resolution."""
    import fnmatch

    aliases = {
        "*sonnet*": "combo_sonnet",
        "*opus*": "combo_opus",
        "*haiku*": "combo_haiku",
    }

    test_cases = [
        ("claude-sonnet-4-20250514", "combo_sonnet"),
        ("claude-3-opus-20240229", "combo_opus"),
        ("claude-3-5-haiku-20241022", "combo_haiku"),
    ]

    for raw_model, expected in test_cases:
        resolved = None
        for pattern, alias in aliases.items():
            if fnmatch.fnmatch(raw_model.lower(), pattern.lower()):
                resolved = alias
                break
        assert resolved == expected, f"FAIL: '{raw_model}' should map to '{expected}', got '{resolved}'"

    print("  [PASS] Alias resolution: wildcard patterns work")


def test_alias_no_match_passthrough():
    """Model not in alias map passes through unchanged."""
    import fnmatch

    aliases = {"*claude*": "combo_sonnet"}
    raw_model = "deepseek-v4"

    resolved = raw_model  # Default: pass through
    for pattern, alias in aliases.items():
        if fnmatch.fnmatch(raw_model.lower(), pattern.lower()):
            resolved = alias
            break

    assert resolved == "deepseek-v4", f"FAIL: should pass through, got {resolved}"
    print("  [PASS] Alias resolution: non-matching model passes through")


# ═══════════════════════════════════════════════════════════════
# PHASE 6: Responses API
# ═══════════════════════════════════════════════════════════════

def test_responses_conversion():
    responses_body = {
        "model": "gpt-4o",
        "instructions": "You are a coding assistant.",
        "input": [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "message", "role": "assistant", "content": "Hi there!"},
        ],
        "tools": [{
            "type": "function",
            "name": "Run",
            "description": "Run a command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}
        }],
        "stream": False,
        "max_output_tokens": 4096,
    }

    chat_body = ResponsesConverter.responses_to_chat(responses_body)

    # Check system message from instructions
    assert chat_body["messages"][0]["role"] == "system"
    assert "coding assistant" in chat_body["messages"][0]["content"]

    # Check messages converted
    assert len(chat_body["messages"]) == 3  # system + user + assistant
    assert chat_body["messages"][1]["role"] == "user"
    assert chat_body["messages"][1]["content"] == "Hello"

    # Check tools converted
    assert "tools" in chat_body
    assert chat_body["tools"][0]["function"]["name"] == "Run"

    # Check max_output_tokens mapped
    assert chat_body["max_tokens"] == 4096

    print("  [PASS] Responses API -> Chat Completions conversion")


def test_responses_with_function_call():
    """Responses format with function_call/function_call_output items."""
    responses_body = {
        "model": "gpt-4o",
        "input": [
            {"type": "message", "role": "user", "content": "What time is it?"},
            {
                "type": "function_call",
                "id": "fc_001",
                "call_id": "call_001",
                "name": "get_time",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_001",
                "output": "12:30 PM",
            },
        ],
    }

    chat_body = ResponsesConverter.responses_to_chat(responses_body)

    # Find assistant tool_calls
    asst_msgs = [m for m in chat_body["messages"] if m["role"] == "assistant"]
    assert len(asst_msgs) == 1
    assert "tool_calls" in asst_msgs[0]
    assert asst_msgs[0]["tool_calls"][0]["id"] == "call_001"
    assert asst_msgs[0]["tool_calls"][0]["function"]["name"] == "get_time"

    # Find tool result
    tool_msgs = [m for m in chat_body["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_001"
    assert tool_msgs[0]["content"] == "12:30 PM"

    print("  [PASS] Responses API: function_call/output converted correctly")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("BSL Router -- Full ACL Test Suite (Phase 1-7)")
    print("=" * 60)

    all_tests = [
        # Phase 1
        ("Phase 1: Tool Preservation", [
            test_anthropic_ingress_preserves_tools,
            test_tool_roundtrip,
        ]),
        # Phase 2
        ("Phase 2: Provider Profile Registry", [
            test_profile_registry_anthropic,
            test_profile_registry_glm,
            test_profile_registry_deepseek,
            test_profile_registry_unknown_provider,
            test_profile_registry_config_override,
        ]),
        # Phase 3
        ("Phase 3: Reasoning Policy Engine", [
            test_detect_family,
            test_glm51_thinking_options,
            test_glm52_thinking_options,
            test_thinking_injection_glm,
            test_thinking_injection_off,
            test_thinking_injection_anthropic_no_inject,
        ]),
        # Phase 4
        ("Phase 4: Stream Normalizer", [
            test_stream_normalizer_needs_conversion,
            test_stream_conversion,
        ]),
        # Tool Ledger
        ("Tool Ledger", [
            test_tool_ledger_clean,
            test_tool_ledger_orphan,
        ]),
        # Claude Code Alias
        ("Claude Code Alias Resolution", [
            test_alias_resolution_exact,
            test_alias_resolution_wildcard,
            test_alias_no_match_passthrough,
        ]),
        # Phase 6
        ("Phase 6: Responses API", [
            test_responses_conversion,
            test_responses_with_function_call,
        ]),
    ]

    total_passed = 0
    total_failed = 0

    for section_name, tests in all_tests:
        section(section_name)
        for test in tests:
            try:
                test()
                total_passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {test.__name__}: {e}")
                total_failed += 1
            except Exception as e:
                print(f"  [ERROR] {test.__name__}: {type(e).__name__}: {e}")
                total_failed += 1

    print(f"\n{'=' * 60}")
    print(f"TOTAL: {total_passed} passed, {total_failed} failed")
    print(f"{'=' * 60}")
    if total_failed > 0:
        sys.exit(1)
