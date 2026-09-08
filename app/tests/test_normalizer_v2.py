"""normalizer_v2 — canonical round-trip and pairing-scan tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.normalizer_v2 import (  # noqa: E402
    CanonicalRequest,
    from_canonical,
    scan_tool_pairing,
    to_canonical,
)


def test_openai_chat_round_trip():
    body = {
        "model": "gpt-5.6-sol",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "look", "arguments": "{\"q\": \"cat\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "a cat"},
        ],
        "tools": [{"type": "function", "function": {"name": "look", "parameters": {}}}],
        "tool_choice": "auto",
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 512,
        "stop": ["\n\n"],
    }
    can = to_canonical("openai-chat", body)
    assert can.model == "gpt-5.6-sol"
    assert can.system == [{"type": "text", "text": "You are terse."}]
    img = can.messages[0]["parts"][1]
    assert img["type"] == "image" and img["data"] == "QUJD" and img["media_type"] == "image/png"
    tu = can.messages[1]["parts"][0]
    assert tu["type"] == "tool_use" and tu["name"] == "look" and tu["arguments"] == {"q": "cat"}
    tr = can.messages[2]["parts"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "call_1"

    out = from_canonical("openai-chat", can)
    assert out["model"] == "gpt-5.6-sol"
    assert out["system"] == "You are terse."
    user_parts = out["messages"][0]["content"]
    assert user_parts[0]["text"] == "describe this"
    img_out = user_parts[1]["image_url"]["url"]
    assert img_out == "data:image/png;base64,QUJD"
    asst = out["messages"][1]
    assert asst["tool_calls"][0]["function"]["name"] == "look"
    assert asst["tool_calls"][0]["id"] == "call_1"
    tool_msg = out["messages"][2]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1" and tool_msg["content"] == "a cat"
    assert out["tool_choice"] == "auto"
    assert out["temperature"] == 0.2 and out["top_p"] == 0.9
    assert out["max_tokens"] == 512 and out["stop"] == ["\n\n"]
    assert out["tools"] == body["tools"]


def test_raw_arguments_preserved_on_invalid_json():
    body = {
        "model": "m",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{Includes: *.js}"}},
                ],
            }
        ],
    }
    can = to_canonical("openai-chat", body)
    tu = can.messages[0]["parts"][0]
    assert tu["arguments"] == {}
    assert tu["raw_arguments"] == "{Includes: *.js}"
    out = from_canonical("openai-chat", can)
    assert out["messages"][0]["tool_calls"][0]["function"]["arguments"] == "{Includes: *.js}"


def test_scan_tool_pairing_unanswered_only():
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "parts": [{"type": "tool_use", "id": "a", "name": "fa", "arguments": {}}]},
        {"role": "user", "parts": [{"type": "tool_result", "tool_use_id": "a", "content": "ok", "is_error": None}]},
        {"role": "assistant", "parts": [{"type": "tool_use", "id": "b", "name": "fb", "arguments": {}}]},
    ]
    result = scan_tool_pairing(msgs)
    assert result == [{"tool_use_id": "b", "name": "fb", "index": 0}]


def test_unknown_dialect_and_malformed_message():
    try:
        to_canonical("nope-dialect", {})
        assert False, "expected ValueError"
    except ValueError:
        pass

    body = {"model": "m", "messages": [{"role": "user", "content": 12345}, {"weird": "shape", "tool_calls": "garbage"}]}
    can = to_canonical("openai-chat", body)  # must not raise
    assert isinstance(can, CanonicalRequest)
    assert can.messages[0]["parts"] == [{"type": "unknown", "raw": 12345}]
    # non-list tool_calls iterates empty -> message degrades, never raises
    assert isinstance(can.messages[1]["parts"], list)


def test_responses_round_trip():
    import json as _json

    body = {
        "model": "gpt-5.6-resp",
        "instructions": "You are a router.",
        "input": [
            {"type": "message", "role": "user", "content": "plain string content"},
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
                ],
            },
            {"type": "function_call", "call_id": "call_9", "name": "look", "arguments": "{\"q\": \"cat\"}"},
            {"type": "function_call_output", "call_id": "call_9", "output": "a cat"},
        ],
        "tools": [{"type": "function", "name": "look", "description": "Look up",
                   "parameters": {"type": "object"}, "strict": True}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "text": {"format": {"type": "json_schema", "json_schema": {"name": "out", "schema": {"type": "object"}}}},
        "max_output_tokens": 768,
        "temperature": 0.4,
        "top_p": 0.8,
    }
    can = to_canonical("responses", body)
    assert can.model == "gpt-5.6-resp"
    assert can.system == [{"type": "text", "text": "You are a router."}]
    assert can.messages[0]["parts"] == [{"type": "text", "text": "plain string content"}]
    img = can.messages[1]["parts"][1]
    assert img["type"] == "image" and img["data"] == "QUJD" and img["media_type"] == "image/png"
    tu = can.messages[2]["parts"][0]
    assert tu["type"] == "tool_use" and tu["id"] == "call_9" and tu["name"] == "look"
    assert tu["arguments"] == {"q": "cat"} and tu["raw_arguments"] == "{\"q\": \"cat\"}"
    tr = can.messages[3]["parts"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "call_9" and tr["content"] == "a cat"
    assert can.tools[0]["function"]["name"] == "look"
    assert can.reasoning["enabled"] is True and can.reasoning["effort"] == "high"
    assert can.max_tokens == 768 and can.temperature == 0.4 and can.top_p == 0.8
    assert can.response_format == body["text"]["format"]

    out = from_canonical("responses", can)
    assert out["instructions"] == "You are a router."
    items = out["input"]
    assert items[0] == {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "plain string content"}]}
    assert items[1]["content"][0] == {"type": "input_text", "text": "describe this"}
    assert items[1]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}
    fc = items[2]
    assert fc["type"] == "function_call" and fc["call_id"] == "call_9" and fc["name"] == "look"
    assert _json.loads(fc["arguments"]) == {"q": "cat"}
    assert items[3] == {"type": "function_call_output", "call_id": "call_9", "output": "a cat"}
    assert out["tools"][0] == {"type": "function", "name": "look", "description": "Look up",
                               "parameters": {"type": "object"}, "strict": True}
    assert out["reasoning"] == {"effort": "high"}
    assert out["text"]["format"] == body["text"]["format"]
    assert out["max_output_tokens"] == 768
    assert out["temperature"] == 0.4 and out["top_p"] == 0.8


def test_gemini_round_trip():
    body = {
        "model": "gemini-3-pro",
        "systemInstruction": {"parts": [{"text": "Be brief."}]},
        "contents": [
            {"role": "user", "parts": [{"text": "find a cat"}]},
            {
                "role": "model",
                "parts": [
                    {"text": "pondering", "thought": True},
                    {"functionCall": {"name": "look", "args": {"q": "cat"}}},
                ],
            },
            {"role": "user", "parts": [{"functionResponse": {"name": "look", "response": {"found": True}}}]},
        ],
        "tools": [{"functionDeclarations": [
            {"name": "look", "description": "Look up", "parameters": {"type": "object"}}
        ]}],
        "generationConfig": {
            "temperature": 0.3,
            "topP": 0.9,
            "stopSequences": ["END"],
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    can = to_canonical("gemini", body)
    assert can.system == [{"type": "text", "text": "Be brief."}]
    assert can.messages[0]["role"] == "user" and can.messages[0]["parts"][0]["text"] == "find a cat"
    m1 = can.messages[1]
    assert m1["role"] == "assistant"
    think = m1["parts"][0]
    assert think["type"] == "thinking" and think["thinking"] == "pondering"
    tu = m1["parts"][1]
    assert tu["type"] == "tool_use" and tu["id"] == "gemini_fc:look"
    assert tu["name"] == "look" and tu["arguments"] == {"q": "cat"}
    tr = can.messages[2]["parts"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "gemini_fc:look"
    assert tr["content"] == {"found": True}
    assert can.tools[0]["function"]["name"] == "look"
    assert can.reasoning == {"enabled": True, "budget_tokens": 1024}
    assert can.temperature == 0.3 and can.top_p == 0.9 and can.stop == ["END"]

    out = from_canonical("gemini", can)
    assert out["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert out["contents"][0] == {"role": "user", "parts": [{"text": "find a cat"}]}
    c1 = out["contents"][1]
    assert c1["role"] == "model"
    assert c1["parts"][0] == {"text": "pondering", "thought": True}
    assert c1["parts"][1] == {"functionCall": {"name": "look", "args": {"q": "cat"}}}
    assert out["contents"][2]["parts"][0] == {"functionResponse": {"name": "look", "response": {"found": True}}}
    assert out["tools"] == body["tools"]
    gen = out["generationConfig"]
    assert gen["temperature"] == 0.3 and gen["topP"] == 0.9
    assert gen["stopSequences"] == ["END"]
    assert gen["thinkingConfig"] == {"thinkingBudget": 1024}


def test_gemini_image_round_trip():
    body = {
        "model": "gemini-3",
        "contents": [
            {"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}},
                {"text": "what is this"},
            ]},
        ],
    }
    can = to_canonical("gemini", body)
    img = can.messages[0]["parts"][0]
    assert img["type"] == "image" and img["data"] == "iVBORw0KGgo="
    assert img["media_type"] == "image/png" and img["url"] is None
    out = from_canonical("gemini", can)
    assert out["contents"][0]["parts"][0] == {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    assert out["contents"][0]["parts"][1] == {"text": "what is this"}


def test_responses_extras_round_trip():
    body = {"model": "m", "input": "hi", "previous_response_id": "resp_42", "store": True}
    can = to_canonical("responses", body)
    assert can.extras["responses"]["previous_response_id"] == "resp_42"
    assert can.extras["responses"]["store"] is True
    out = from_canonical("responses", can)
    assert out["previous_response_id"] == "resp_42"
    assert out["store"] is True
    assert out["input"][0]["content"][0] == {"type": "input_text", "text": "hi"}


def test_unknown_items_both_dialects():
    resp_body = {"model": "m", "input": [{"type": "reference", "id": "ref_1"}, "bare string item"]}
    can = to_canonical("responses", resp_body)  # must not raise
    parts = [p for m in can.messages for p in m["parts"]]
    assert {"type": "unknown", "raw": {"type": "reference", "id": "ref_1"}} in parts
    assert {"type": "unknown", "raw": "bare string item"} in parts
    out = from_canonical("responses", can)  # no raise; raw rides along verbatim
    assert {"type": "reference", "id": "ref_1"} in out["input"]

    gem_body = {"model": "g", "contents": [{"role": "user", "parts": [{"codeExecutionResult": {"output": "42"}}]}]}
    can2 = to_canonical("gemini", gem_body)  # must not raise
    assert can2.messages[0]["parts"][0] == {"type": "unknown", "raw": {"codeExecutionResult": {"output": "42"}}}
    out2 = from_canonical("gemini", can2)
    assert out2["contents"][0]["parts"][0] == {"codeExecutionResult": {"output": "42"}}


def test_old_suite_still_green():
    test_openai_chat_round_trip()
    test_raw_arguments_preserved_on_invalid_json()
    test_scan_tool_pairing_unanswered_only()
    test_unknown_dialect_and_malformed_message()
