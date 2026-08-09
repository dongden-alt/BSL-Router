"""
Phase 5B-1 — Antigravity (Google Cloud Code / Gemini private API) golden tests.

Pure-function tests for app/compat/adapters/gemini.py: the Gemini↔OpenAI
converter that ports 9Router's Antigravity MITM conversion. No live network.

Run: .venv\\Scripts\\python -m pytest app/tests/test_antigravity_5b1.py -q

Authoritative reference: .brain/harvest/antigravity_conversion_spec.md
"""
import copy
import json
import os
import sys

# Add project root to path (mirrors the sibling test_acl_phase1.py style).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.adapters import (
    unwrap_request,
    is_antigravity,
    normalize_model,
    gemini_request_to_openai,
    openai_chunk_to_gemini,
    openai_response_to_gemini,
    sse_data,
    SSE_DONE,
)


# ─── Shared golden fixtures ───────────────────────────────────────────────────

def _wrapper(contents, **req_extra):
    """Build a Cloud Code envelope around an inner `request` object."""
    request = {"contents": contents}
    request.update(req_extra)
    return {
        "project": "bright-fuze-a1b2c",
        "model": "gemini-3.1-pro-high",
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": "agent-abc-123",
        "request": request,
    }


# ─── T1: request unwrap + convert (system + generationConfig clamp) ──────────

def test_t1_unwrap_and_convert_basic_request():
    """Wrapper with text contents + systemInstruction + generationConfig maps to
    an OpenAI body with a leading system message, a clamped max_tokens, and
    temperature/top_p carried through."""
    wrapper = _wrapper(
        [{"role": "user", "parts": [{"text": "hello world"}]}],
        systemInstruction={"role": "user", "parts": [{"text": "You are helpful."}]},
        generationConfig={
            "temperature": 1,
            "topP": 0.95,
            "topK": 40,
            "maxOutputTokens": 99999,  # above the 16384 cap → must clamp (§8.4)
        },
    )

    assert is_antigravity(wrapper) is True
    inner = unwrap_request(wrapper)
    assert inner is wrapper["request"]  # unwrapped to the inner request object

    openai_body = gemini_request_to_openai(inner, normalize_model(wrapper["model"]))

    # Model synonym applied (§6).
    assert openai_body["model"] == "gemini-pro-agent"

    # System message first, then the user message.
    assert openai_body["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert openai_body["messages"][1]["role"] == "user"
    assert openai_body["messages"][1]["content"] == "hello world"

    # Scalars: clamp + carry-through.
    assert openai_body["max_tokens"] == 16384
    assert openai_body["temperature"] == 1
    assert openai_body["top_p"] == 0.95

    # Default stream flag is True (§8.12); the route overrides it from the path.
    assert openai_body["stream"] is True

    # safetySettings are never carried (§8.5).
    assert "safetySettings" not in openai_body


def test_t1_thinking_budget_maps_to_reasoning_effort():
    """thinkingConfig.thinkingBudget → reasoning_effort via Vp thresholds (§3)."""
    for budget, expected in [(1024, "low"), (8192, "medium"), (32000, "high")]:
        wrapper = _wrapper(
            [{"role": "user", "parts": [{"text": "x"}]}],
            generationConfig={"thinkingConfig": {"thinkingBudget": budget}},
        )
        ob = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))
        assert ob["reasoning_effort"] == expected, f"budget {budget} → {ob.get('reasoning_effort')}"


# ─── T2: tool request (functionDeclarations + VALIDATED toolConfig) ──────────

def test_t2_tool_request_function_declarations():
    """functionDeclarations → OpenAI tools; schema type lowercased + enumDescriptions
    stripped; toolConfig records the VALIDATED mode (§3, §8.6, §8.7)."""
    wrapper = _wrapper(
        [{"role": "user", "parts": [{"text": "what's the weather?"}]}],
        tools=[{
            "functionDeclarations": [{
                "name": "get_weather",
                "description": "Get the weather for a city.",
                "parameters": {
                    "type": "OBJECT",
                    "enumDescriptions": ["a", "b"],
                    "properties": {"city": {"type": "STRING"}},
                },
            }],
        }],
        toolConfig={"functionCallingConfig": {"mode": "VALIDATED"}},
    )

    ob = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))

    assert "tools" in ob
    tool = ob["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "get_weather"
    # type lowercased, enumDescriptions stripped (§3).
    assert tool["function"]["parameters"]["type"] == "object"
    assert "enumDescriptions" not in tool["function"]["parameters"]
    # VALIDATED mode recorded when tools present (§8.6).
    assert ob["x_gemini_tool_mode"] == "VALIDATED"


def test_t2_synthetic_schema_when_parameters_missing():
    """A functionDeclaration with no parameters gets a synthetic reason schema (§8.7)."""
    wrapper = _wrapper(
        [{"role": "user", "parts": [{"text": "go"}]}],
        tools=[{"functionDeclarations": [{"name": "noop", "description": "no args"}]}],
        toolConfig={"functionCallingConfig": {"mode": "VALIDATED"}},
    )
    ob = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))
    params = ob["tools"][0]["function"]["parameters"]
    assert params["type"] == "object"
    assert "reason" in params["properties"]
    assert params["required"] == ["reason"]


def test_t2_nested_schema_types_are_lowercased_without_mutating_input():
    """Nested schemas normalize type strings and strip enumDescriptions everywhere."""
    parameters = {
        "type": "OBJECT",
        "properties": {
            "enabled": {"type": "BOOLEAN", "enumDescriptions": ["on", "off"]},
            "steps": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {"label": {"type": "STRING", "enumDescriptions": ["step"]}},
                },
            },
        },
        "anyOf": [{"type": "STRING"}, {"type": "BOOLEAN", "enumDescriptions": ["flag"]}],
        "oneOf": [{"type": "ARRAY", "items": {"type": "STRING"}}],
        "allOf": [{"type": "OBJECT", "properties": {"count": {"type": "STRING"}}}],
        "$defs": {
            "result": {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}}
        },
        "description": "BOOLEAN must remain unchanged outside type fields.",
    }
    original = copy.deepcopy(parameters)
    wrapper = _wrapper(
        [{"role": "user", "parts": [{"text": "go"}]}],
        tools=[{"functionDeclarations": [{"name": "nested", "parameters": parameters}]}],
    )

    params = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))["tools"][0]["function"]["parameters"]

    assert params["type"] == "object"
    assert params["properties"]["enabled"]["type"] == "boolean"
    assert params["properties"]["steps"]["type"] == "array"
    assert params["properties"]["steps"]["items"]["type"] == "object"
    assert params["properties"]["steps"]["items"]["properties"]["label"]["type"] == "string"
    assert params["anyOf"][0]["type"] == "string"
    assert params["anyOf"][1]["type"] == "boolean"
    assert params["oneOf"][0]["type"] == "array"
    assert params["oneOf"][0]["items"]["type"] == "string"
    assert params["allOf"][0]["type"] == "object"
    assert params["$defs"]["result"]["properties"]["ok"]["type"] == "boolean"
    assert "enumDescriptions" not in json.dumps(params)
    assert params["description"] == "BOOLEAN must remain unchanged outside type fields."
    assert parameters == original


# ─── T3: functionCall / functionResponse round-trip ──────────────────────────

def test_t3_function_call_and_response_round_trip():
    """Gemini functionCall→assistant tool_calls; functionResponse→tool-role message
    with re-roled content (§3, §8.1)."""
    wrapper = _wrapper([
        {"role": "user", "parts": [{"text": "weather in NYC?"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "NYC"}}}]},
        {"role": "model", "parts": [{"functionResponse": {"name": "get_weather", "response": {"result": "sunny"}}}]},
    ])
    ob = gemini_request_to_openai(unwrap_request(wrapper), normalize_model(wrapper["model"]))

    roles = [m["role"] for m in ob["messages"]]
    assert roles == ["user", "assistant", "tool"]

    # Assistant turn carries the tool_call with JSON-stringified arguments.
    asst = ob["messages"][1]
    assert asst["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"city": "NYC"}

    # functionResponse → OpenAI tool message (§3). The id now carries a
    # per-invocation suffix so it is globally unique (Anthropic requirement),
    # and the tool message MUST reference the SAME id the assistant emitted.
    call_id = asst["tool_calls"][0]["id"]
    assert call_id == "call_get_weather_1"
    tool_msg = ob["messages"][2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == call_id  # round-trip pairing preserved
    assert json.loads(tool_msg["content"]) == "sunny"

    # §8.1: a functionResponse-bearing content is re-roled to user, but since it
    # produces ONLY a tool message, no spurious empty user message follows.
    assert not any(
        m["role"] == "user" and (m.get("content") in (None, ""))
        for m in ob["messages"][3:]
    )


def test_t3_repeated_tool_calls_get_unique_ids():
    """Regression (pix4k 502 / bare-origin 400): repeated invocations of the SAME
    tool must each get a UNIQUE tool_call id that pairs 1:1 with its response.

    Gemini's wire carries no per-call id; the old converter minted f"call_{name}"
    so a long agent session produced many identical ids (e.g. 30x
    "call_run_command"). Anthropic requires globally-unique tool_use.id, so the
    duplicates made the /v1/messages body malformed — Cloudflare-fronted pix4k
    returned a 502 HTML page and bare origins returned a 400.
    """
    contents = [{"role": "user", "parts": [{"text": "do three things"}]}]
    for i in range(3):
        contents.append({"role": "model", "parts": [{"functionCall": {"name": "run_command", "args": {"cmd": f"echo {i}"}}}]})
        contents.append({"role": "user", "parts": [{"functionResponse": {"name": "run_command", "response": {"result": f"out{i}"}}}]})
    ob = gemini_request_to_openai(unwrap_request(_wrapper(contents)), normalize_model("gemini-3.1-pro-high"))

    call_ids = [m["tool_calls"][0]["id"] for m in ob["messages"] if m.get("tool_calls")]
    tool_ids = [m["tool_call_id"] for m in ob["messages"] if m["role"] == "tool"]

    # Three calls, three responses.
    assert len(call_ids) == 3 and len(tool_ids) == 3
    # All ids globally unique (the core fix).
    assert len(set(call_ids)) == 3, f"duplicate call ids: {call_ids}"
    # Each response paired to the matching call id, in order.
    assert call_ids == tool_ids, f"pairing mismatch: calls={call_ids} results={tool_ids}"


def test_t3_parallel_same_turn_calls_pair_by_name():
    """Two DIFFERENT tools called in one model turn, answered in one user turn,
    must each pair to their own unique id (name-aware FIFO)."""
    contents = [
        {"role": "user", "parts": [{"text": "go"}]},
        {"role": "model", "parts": [
            {"functionCall": {"name": "grep_search", "args": {"q": "a"}}},
            {"functionCall": {"name": "run_command", "args": {"cmd": "b"}}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "run_command", "response": {"result": "rb"}}},
            {"functionResponse": {"name": "grep_search", "response": {"result": "ra"}}},
        ]},
    ]
    ob = gemini_request_to_openai(unwrap_request(_wrapper(contents)), normalize_model("gemini-3.1-pro-high"))

    asst = next(m for m in ob["messages"] if m.get("tool_calls"))
    id_by_name = {tc["function"]["name"]: tc["id"] for tc in asst["tool_calls"]}
    assert len(set(id_by_name.values())) == 2  # unique per tool

    tool_msgs = [m for m in ob["messages"] if m["role"] == "tool"]
    # Responses were given out of call order; name-aware pairing must still match.
    result_content_by_id = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_msgs}
    assert result_content_by_id[id_by_name["run_command"]] == "rb"
    assert result_content_by_id[id_by_name["grep_search"]] == "ra"


# ─── T4: stream chunk (content delta → Gemini part, SSE shape) ───────────────

def test_t4_stream_content_chunk_to_gemini_part():
    """OpenAI delta.content chunk → {response:{candidates[0].content.parts[0].text}};
    SSE frame has NO event: prefix; sse_data shape correct; SSE_DONE terminal."""
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-x",
        "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
    }
    state = {}
    g = openai_chunk_to_gemini(chunk, state)
    assert g is not None
    assert "response" in g

    resp = g["response"]
    cand = resp["candidates"][0]
    assert cand["content"]["role"] == "model"
    assert cand["content"]["parts"][0] == {"text": "Hello"}
    assert resp["responseId"] == "chatcmpl-1"
    assert resp["modelVersion"] == "gpt-x"
    # No finishReason on a mid-stream content chunk.
    assert "finishReason" not in cand

    # SSE wire shape — spec §5: plain data: line, NO event: prefix.
    frame = sse_data(resp)
    assert frame.startswith(b"data: ")
    assert frame.endswith(b"\n\n")
    assert b"event:" not in frame
    assert SSE_DONE == b"data: [DONE]\n\n"


def test_t4_empty_mid_stream_delta_emits_nothing():
    """§4a: a delta with no content and no finish emits nothing (None)."""
    chunk = {
        "id": "x", "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    }
    assert openai_chunk_to_gemini(chunk, {}) is None


# ─── T5: finish chunk (finishReason STOP + usageMetadata + empty-parts→text) ─

def test_t5_finish_chunk_stop_with_usage():
    """finish_reason=stop → finishReason STOP + usageMetadata mapping (§4a, §8.14)."""
    chunk = {
        "id": "chatcmpl-1",
        "model": "gpt-x",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }
    state = {}
    g = openai_chunk_to_gemini(chunk, state)
    resp = g["response"]
    cand = resp["candidates"][0]
    assert cand["finishReason"] == "STOP"
    # Empty parts on a finish chunk → {"text": ""} (§8.8).
    assert cand["content"]["parts"] == [{"text": ""}]
    um = resp["usageMetadata"]
    assert um["promptTokenCount"] == 12
    assert um["candidatesTokenCount"] == 2
    assert um["totalTokenCount"] == 14
    # §8.14: reasoning/cached extras forwarded when present.
    assert um["thoughtsTokenCount"] == 5
    assert um["cachedContentTokenCount"] == 3


def test_t5_finish_reason_remap():
    """OpenAI finish reasons map to Gemini finishReason (§4a): LENGTH→MAX_TOKENS,
    CONTENT_FILTER→SAFETY, TOOL_CALLS→STOP, default→STOP."""
    cases = [("length", "MAX_TOKENS"), ("content_filter", "SAFETY"), ("tool_calls", "STOP"), ("stop", "STOP")]
    for fr, expected in cases:
        chunk = {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": fr}]}
        g = openai_chunk_to_gemini(chunk, {})
        assert g["response"]["candidates"][0]["finishReason"] == expected, f"{fr}→{expected}"


def test_t5_streaming_tool_call_accumulates_then_emits_function_call():
    """Accumulated tool_call deltas across chunks flush to a functionCall part on
    finish; TOOL_CALLS finish maps back to STOP (§4a)."""
    state = {}
    chunks = [
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"city":"NYC"}'}}
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    emitted = [openai_chunk_to_gemini(c, state) for c in chunks]
    # Argument deltas emit nothing until finish.
    assert emitted[0] is None and emitted[1] is None
    finish = emitted[2]["response"]
    part = finish["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"]["name"] == "get_weather"
    assert part["functionCall"]["args"] == {"city": "NYC", "toolSummary": "Executing get_weather", "toolAction": "Running get_weather"}
    assert finish["candidates"][0]["finishReason"] == "STOP"


def test_t5_truncated_tool_call_drops_and_signals_max_tokens():
    """L3 TRUNCATION GUARD: a tool call whose argument JSON is cut off mid-stream
    (thinking model exhausts the output budget) must NOT be emitted as an
    argument-less functionCall (which the IDE reports as TOOL_CALL_INCOMPLETE).
    Instead the malformed call is dropped and finishReason is forced to MAX_TOKENS."""
    state = {}
    chunks = [
        {"id": "x", "model": "claude-opus-4-6-thinking", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "write_to_file", "arguments": ""}}
        ]}, "finish_reason": None}]},
        # Truncated argument JSON — unterminated string, will not parse.
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"TargetFile": "D:\\\\Projects\\\\'}}
        ]}, "finish_reason": None}]},
        # Upstream may even falsely claim tool_calls; guard must override to MAX_TOKENS.
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    emitted = [openai_chunk_to_gemini(c, state) for c in chunks]
    finish = emitted[2]["response"]
    cand = finish["candidates"][0]
    # No functionCall part should survive (the malformed call is dropped).
    assert not any("functionCall" in p for p in cand["content"]["parts"]), cand["content"]["parts"]
    # §8.8 keeps parts non-empty with a text placeholder.
    assert cand["content"]["parts"] == [{"text": ""}]
    # Honest truncation signal, NOT STOP.
    assert cand["finishReason"] == "MAX_TOKENS"


def test_t5_complete_tool_call_still_emits_function_call_after_guard():
    """Regression: the L3 guard must not affect well-formed tool calls — a complete
    argument JSON still flushes to a functionCall with real args and STOP."""
    state = {}
    chunks = [
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"city":"NYC"}'}}
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    emitted = [openai_chunk_to_gemini(c, state) for c in chunks]
    part = emitted[2]["response"]["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"]["name"] == "get_weather"
    assert part["functionCall"]["args"] == {"city": "NYC", "toolSummary": "Executing get_weather", "toolAction": "Running get_weather"}
    assert emitted[2]["response"]["candidates"][0]["finishReason"] == "STOP"


# ─── T6: model key mapping (synonyms + regex + tab passthrough) ───────────────

def test_t6_model_mapping_synonyms_and_regex_and_tab():
    """§6: synonym map first, then regex cascade, then passthrough; tab[_-]* bypass."""
    # Synonyms.
    assert normalize_model("gemini-3.1-pro-high") == "gemini-pro-agent"
    assert normalize_model("gemini-3-pro-high") == "gemini-pro-agent"
    assert normalize_model("gemini-default") == "gemini-3.5-flash-low"
    assert normalize_model("gemini-3.5-flash-high") == "gemini-3-flash-agent"
    # Regex cascade.
    assert normalize_model("gemini-flash-medium-foo") == "gemini-3.5-flash-low"
    assert normalize_model("opus-mega") == "claude-opus-4-6-thinking"
    # Tab-completion passthrough.
    assert normalize_model("tab-complete") == "tab-complete"
    assert normalize_model("tab_foo") == "tab_foo"
    # Unknown id with no pattern hit → passthrough (BSL routing resolves further).
    assert normalize_model("some-unknown-model-id") == "some-unknown-model-id"


# ─── Bonus: non-stream response shape (§4b) ───────────────────────────────────

def test_non_stream_response_shape():
    """Non-stream OpenAI completion → wrapped {response:{candidates, usageMetadata,
    modelVersion, responseId}} (§4b)."""
    openai_resp = {
        "id": "chatcmpl-9",
        "model": "gpt",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    out = openai_response_to_gemini(openai_resp, "gemini-pro-agent")
    assert "response" in out
    resp = out["response"]
    assert resp["candidates"][0]["content"]["parts"][0] == {"text": "Hi there"}
    assert resp["candidates"][0]["finishReason"] == "STOP"
    assert resp["candidates"][0]["index"] == 0
    assert resp["usageMetadata"]["totalTokenCount"] == 7
    assert resp["modelVersion"] == "gemini-pro-agent"
    assert resp["responseId"] == "chatcmpl-9"


def test_non_stream_empty_content_keeps_parts_nonempty():
    """§4b / §8.8: even with empty content, candidates[0].content.parts is non-empty."""
    openai_resp = {
        "id": "x", "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": {},
    }
    out = openai_response_to_gemini(openai_resp, "m")
    parts = out["response"]["candidates"][0]["content"]["parts"]
    assert parts and parts[0] == {"text": ""}


if __name__ == "__main__":
    # Allow direct execution for quick local checks.
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
