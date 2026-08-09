"""
Phase 1 ACL smoke test — verifies tool preservation through the
Anthropic → OpenAI → Anthropic round-trip.

Run: .venv\Scripts\python -m app.tests.test_acl_phase1
"""
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.normalizer import UniversalNormalizer
from app.models import ChatCompletionRequest


def test_anthropic_ingress_preserves_tools():
    """Claude Code sends tools — they must survive normalization."""
    anthropic_request = {
        "model": "glm-4.6",
        "max_tokens": 4096,
        "system": "You are a helpful coding assistant.",
        "messages": [
            {
                "role": "user",
                "content": "Read the file app/main.py"
            }
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file from the filesystem",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "The file to read"}
                    },
                    "required": ["file_path"]
                }
            },
            {
                "name": "Bash",
                "description": "Execute a bash command",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"}
                    },
                    "required": ["command"]
                }
            }
        ],
        "tool_choice": {"type": "auto"},
        "stream": True
    }

    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(anthropic_request)

    # Assert tools preserved
    assert "tools" in openai_body, "FAIL: tools dropped during Anthropic ingress!"
    assert len(openai_body["tools"]) == 2, f"FAIL: expected 2 tools, got {len(openai_body['tools'])}"

    # Assert tool_choice preserved
    assert "tool_choice" in openai_body, "FAIL: tool_choice dropped!"
    assert openai_body["tool_choice"] == "auto", f"FAIL: tool_choice wrong: {openai_body['tool_choice']}"

    # Assert tool format converted to OpenAI
    t0 = openai_body["tools"][0]
    assert t0["type"] == "function", f"FAIL: tool type not function: {t0}"
    assert t0["function"]["name"] == "Read", f"FAIL: tool name wrong: {t0['function']['name']}"
    assert "input_schema" not in t0["function"], "FAIL: Anthropic field leaked into OpenAI tool"
    assert "parameters" in t0["function"], "FAIL: parameters missing"

    # Assert system prompt preserved
    assert len(openai_body["messages"]) == 2, f"Expected 2 messages, got {len(openai_body['messages'])}"
    assert openai_body["messages"][0]["role"] == "system", "FAIL: system role missing"

    print("  [PASS] Anthropic ingress preserves tools + tool_choice")


def test_tool_use_tool_result_roundtrip():
    """Tool_use and tool_result blocks must survive the full round-trip."""
    anthropic_request = {
        "model": "glm-4.6",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "What is 2+2?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me calculate that."},
                    {"type": "tool_use", "id": "toolu_01ABC", "name": "Bash", "input": {"command": "echo 4"}}
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01ABC", "content": "4"}
                ]
            }
        ],
        "stream": False
    }

    # Step 1: Anthropic → OpenAI
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(anthropic_request)
    messages = openai_body["messages"]

    # Find the assistant message with tool_calls
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, f"Expected 1 assistant msg, got {len(assistant_msgs)}"
    asst = assistant_msgs[0]
    assert "tool_calls" in asst, "FAIL: assistant tool_calls dropped!"
    assert len(asst["tool_calls"]) == 1, "FAIL: expected 1 tool_call"
    assert asst["tool_calls"][0]["id"] == "toolu_01ABC", f"FAIL: tool ID changed: {asst['tool_calls'][0]['id']}"
    assert asst["tool_calls"][0]["function"]["name"] == "Bash", "FAIL: tool name wrong"

    # Find the tool-role message
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1, f"Expected 1 tool msg, got {len(tool_msgs)}"
    assert tool_msgs[0]["tool_call_id"] == "toolu_01ABC", f"FAIL: tool_call_id mismatch: {tool_msgs[0]['tool_call_id']}"
    assert tool_msgs[0]["content"] == "4", f"FAIL: tool result content wrong: {tool_msgs[0]['content']}"
    assert tool_msgs[0]["name"] == "Bash", f"FAIL: tool name mismatch, expected 'Bash', got '{tool_msgs[0].get('name')}'"

    print("  [PASS] tool_use/tool_result preserved through Anthropic -> OpenAI ingress")

    # Step 2: OpenAI → Anthropic (outbound to GLM/Kimi/MiniMax)
    internal_request = ChatCompletionRequest.model_validate(openai_body)
    anthropic_out = UniversalNormalizer.normalize_to_anthropic(internal_request)

    # Assert tools would be in outbound (this request has no tools, but test the path)
    out_messages = anthropic_out["messages"]

    # Find assistant tool_use in outbound
    out_asst = [m for m in out_messages if m["role"] == "assistant"]
    assert len(out_asst) == 1
    out_blocks = out_asst[0]["content"]
    tool_use_blocks = [b for b in out_blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    assert len(tool_use_blocks) == 1, f"FAIL: expected 1 tool_use in outbound, got {len(tool_use_blocks)}"
    assert tool_use_blocks[0]["id"] == "toolu_01ABC", "FAIL: tool ID changed in outbound!"
    assert tool_use_blocks[0]["name"] == "Bash", "FAIL: tool name changed in outbound"
    assert tool_use_blocks[0]["input"] == {"command": "echo 4"}, f"FAIL: tool input changed: {tool_use_blocks[0]['input']}"

    # Find tool_result in outbound
    out_user_msgs = [m for m in out_messages if m["role"] == "user"]
    result_found = False
    for um in out_user_msgs:
        if isinstance(um.get("content"), list):
            for block in um["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    assert block["tool_use_id"] == "toolu_01ABC", "FAIL: tool_result ID mismatch in outbound"
                    result_found = True
    assert result_found, "FAIL: tool_result block missing from outbound!"

    print("  [PASS] tool_use/tool_result preserved through OpenAI -> Anthropic outbound")


def test_tools_in_outbound():
    """Tools must appear in the Anthropic outbound payload."""
    openai_body = {
        "model": "glm-4.6",
        "messages": [{"role": "user", "content": "test"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}
                }
            }
        ],
        "tool_choice": "auto",
        "stream": True
    }

    internal_request = ChatCompletionRequest.model_validate(openai_body)
    anthropic_out = UniversalNormalizer.normalize_to_anthropic(internal_request)

    assert "tools" in anthropic_out, "FAIL: tools missing from Anthropic outbound!"
    assert len(anthropic_out["tools"]) == 1, f"FAIL: expected 1 tool in outbound"
    assert anthropic_out["tools"][0]["name"] == "Read", "FAIL: tool name wrong in outbound"
    assert "input_schema" in anthropic_out["tools"][0], "FAIL: input_schema missing from outbound"
    assert "parameters" not in anthropic_out["tools"][0], "FAIL: OpenAI field leaked"

    assert "tool_choice" in anthropic_out, "FAIL: tool_choice missing from outbound!"
    assert anthropic_out["tool_choice"]["type"] == "auto", f"FAIL: tool_choice wrong: {anthropic_out['tool_choice']}"

    print("  [PASS] Tools + tool_choice preserved in Anthropic outbound")


def test_tool_choice_any():
    """tool_choice type:any → required"""
    anthropic_request = {
        "model": "test",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": {"type": "any"},
    }
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(anthropic_request)
    assert openai_body.get("tool_choice") == "required", f"FAIL: 'any' should map to 'required', got {openai_body.get('tool_choice')}"

    internal_request = ChatCompletionRequest.model_validate(openai_body)
    anthropic_out = UniversalNormalizer.normalize_to_anthropic(internal_request)
    assert anthropic_out.get("tool_choice", {}).get("type") == "any", f"FAIL: 'required' should map back to 'any'"

    print("  [PASS] tool_choice any <-> required round-trip")


if __name__ == "__main__":
    print("=" * 60)
    print("BSL Router — Phase 1 ACL Smoke Tests")
    print("=" * 60)

    tests = [
        test_anthropic_ingress_preserves_tools,
        test_tool_use_tool_result_roundtrip,
        test_tools_in_outbound,
        test_tool_choice_any,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    if failed > 0:
        sys.exit(1)
