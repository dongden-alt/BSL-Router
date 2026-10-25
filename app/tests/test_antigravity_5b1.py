"""
Phase 5B-1 — Antigravity (Google Cloud Code / Gemini private API) golden tests.

Pure-function tests for app/compat/adapters/gemini.py: the Gemini↔OpenAI
converter that ports 9Router's Antigravity MITM conversion. No live network.

Run: .venv\\Scripts\\python -m pytest app/tests/test_antigravity_5b1.py -q

Authoritative reference: internal conversion spec, local .brain
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
    BSL_NO_OUTPUT_NOTICE,
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
    assert SSE_DONE == b": [DONE]\n\n"


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
    # §8.8 (2026-08-24, v3): empty finish parts carry empty text (not notice).
    # The frame is emitted (finishReason + usageMetadata), but has no renderable content.
    assert cand["content"]["parts"][0]["text"] == "", cand["content"]["parts"]
    assert cand["content"]["parts"][0]["text"] != BSL_NO_OUTPUT_NOTICE
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
    """DROP-SYNDROME FIX (2026-09-22): a tool call whose argument JSON is cut
    mid-stream is now REPAIRED via the shared ladder before being dropped.

    This test was written when truncated args were masked as args={} (causing
    TOOL_CALL_INCOMPLETE), then updated when the guard started dropping them.
    Now the guard repairs first: the fragment `{\"TargetFile\": \"D:\\Projects\\`
    closes to `{\"TargetFile\": \"D:\\Projects\\\"}`, which is valid JSON.

    The call SURVIVES with repaired args and finishReason=STOP. A call is
    dropped and MAX_TOKENS forced ONLY when the fragment is genuinely
    unrecoverable (see test_gemini_tool_arg_repair.py::TestUnrecoverableStillDrops).
    """
    state = {}
    chunks = [
        {"id": "x", "model": "claude-opus-5-5-high", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "write_to_file", "arguments": ""}}
        ]}, "finish_reason": None}]},
        # Truncated argument JSON — unterminated string, but REPAIRABLE.
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"TargetFile": "D:\\\\Projects\\\\'}}
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    emitted = [openai_chunk_to_gemini(c, state) for c in chunks]
    finish = emitted[2]["response"]
    cand = finish["candidates"][0]
    # The call now SURVIVES with repaired args instead of being dropped.
    fc_parts = [p for p in cand["content"]["parts"] if "functionCall" in p]
    assert len(fc_parts) == 1, "repairable call must not be dropped"
    assert fc_parts[0]["functionCall"]["name"] == "write_to_file"
    assert fc_parts[0]["functionCall"]["args"]["TargetFile"] == 'D:\\Projects\\"}'
    # A complete batch (all calls recovered) must NOT signal truncation.
    assert cand["finishReason"] == "STOP", "repaired batch must not force MAX_TOKENS"


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
    assert normalize_model("opus-mega") == "claude-opus-5-5-high"
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
    """§4b / §8.8 (2026-08-24): empty content yields a VISIBLE notice part —
    the IDE rejects text+tool_calls both empty."""
    openai_resp = {
        "id": "x", "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": {},
    }
    out = openai_response_to_gemini(openai_resp, "m")
    parts = out["response"]["candidates"][0]["content"]["parts"]
    assert parts and parts[0]["text"].startswith("[BSL Router]"), parts
    assert parts[0]["text"].strip() != ""


def test_protojson_stringified_schema_integers_coerced_across_all_providers():
    """Regression (2026-10-26): Antigravity's Go `protojson` serializes int64
    fields in `learning.genai.beyond.Schema` (`minLength`, `maxLength`,
    `minItems`, `maxItems`, `minProperties`, `maxProperties`) as JSON strings
    (e.g. `send_message` carries `"minLength": "1"` on `Recipient` and `Message`).
    Strict JSON Schema validators (`agentrouter/gpt-5.6-sol`, `codex/gpt-5.6-sol`,
    Anthropic, Kiro, etc.) reject `"1"` with:
      `Invalid schema for function 'send_message': '1' is not of type 'integer'`.
    Verify all ingress/egress normalizers coerce stringified numeric constraints
    without corrupting property names that happen to match schema keywords.
    """
    from app.normalizer import UniversalNormalizer
    from app import codex_adapter, kiro_adapter, normalizer_v2
    from app.compat.adapters.antigravity_upstream import _normalize_tool_parameters

    send_message_params = {
        "type": "OBJECT",
        "properties": {
            "Recipient": {
                "type": "STRING",
                "description": "Conversation ID of the agent to message.",
                "minLength": "1",
                "maxLength": "128",
            },
            "Message": {
                "type": "STRING",
                "description": "Content of the message.",
                "minLength": "1",
            },
            "Tags": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "minItems": "1",
                "maxItems": "3",
            },
            "Timeout": {
                "type": "INTEGER",
                "minimum": "0",
                "maximum": "30000",
                "multipleOf": "0.5",
            },
            # Property whose user-defined name collides with a numeric schema keyword:
            "minLength": {
                "type": "STRING",
                "minLength": "2",
            },
        },
        "minProperties": "1",
        "maxProperties": "10",
        "required": ["Recipient", "Message"],
    }
    wrapper = _wrapper(
        [{"role": "user", "parts": [{"text": "compact"}]}],
        tools=[{
            "functionDeclarations": [
                {"name": "send_message", "description": "Send message", "parameters": send_message_params},
                {"name": "json_schema_decl", "parametersJsonSchema": {"type": "OBJECT", "properties": {"x": {"type": "STRING", "minLength": "4"}}}},
            ]
        }],
    )
    ob = gemini_request_to_openai(unwrap_request(wrapper), "gpt-5.6-sol")
    p = ob["tools"][0]["function"]["parameters"]
    assert p["properties"]["Recipient"]["minLength"] == 1
    assert isinstance(p["properties"]["Recipient"]["minLength"], int)
    assert p["properties"]["Recipient"]["maxLength"] == 128
    assert p["properties"]["Message"]["minLength"] == 1
    assert p["properties"]["Tags"]["minItems"] == 1
    assert p["properties"]["Tags"]["maxItems"] == 3
    assert p["properties"]["Timeout"]["minimum"] == 0
    assert p["properties"]["Timeout"]["maximum"] == 30000
    assert p["properties"]["Timeout"]["multipleOf"] == 0.5
    assert p["minProperties"] == 1
    assert p["maxProperties"] == 10
    # Property literally named "minLength" is preserved as a subschema dict:
    assert isinstance(p["properties"]["minLength"], dict)
    assert p["properties"]["minLength"]["type"] == "string"
    assert p["properties"]["minLength"]["minLength"] == 2
    # parametersJsonSchema fallback also sanitized:
    p2 = ob["tools"][1]["function"]["parameters"]
    assert p2["properties"]["x"]["minLength"] == 4

    # Direct OpenAI ingress (UniversalNormalizer.normalize_to_openai) with raw stringified minLength:
    raw_openai = {
        "model": "agentrouter/gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "send_message", "parameters": copy.deepcopy(send_message_params)}}],
    }
    internal_req = UniversalNormalizer.normalize_to_openai(raw_openai)
    assert internal_req.tools[0]["function"]["parameters"]["properties"]["Recipient"]["minLength"] == 1

    # Anthropic egress & ingress:
    anth_payload = UniversalNormalizer.normalize_to_anthropic(internal_req)
    assert anth_payload["tools"][0]["input_schema"]["properties"]["Recipient"]["minLength"] == 1
    anth_roundtrip = UniversalNormalizer.normalize_to_openai_from_anthropic({
        "model": "claude",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "send_message", "input_schema": copy.deepcopy(send_message_params)}],
    })
    assert anth_roundtrip["tools"][0]["function"]["parameters"]["properties"]["Recipient"]["minLength"] == 1

    # Codex Responses egress:
    codex_body = codex_adapter.openai_to_responses(copy.deepcopy(raw_openai))
    assert codex_body["tools"][0]["parameters"]["properties"]["Recipient"]["minLength"] == 1

    # Kiro egress:
    kiro_tools = kiro_adapter._convert_tools_v2(raw_openai["tools"])
    assert kiro_tools[0]["toolSpecification"]["inputSchema"]["json"]["properties"]["Recipient"]["minLength"] == 1

    # normalizer_v2 Gemini ingress:
    can = normalizer_v2.to_canonical(normalizer_v2.DIALECT_GEMINI, unwrap_request(wrapper))
    assert can.tools[0]["function"]["parameters"]["properties"]["Recipient"]["minLength"] == 1

    # Antigravity Cloud Code upstream egress (coerces allowlisted minItems/maxItems/minimum/maximum):
    ag_params = _normalize_tool_parameters(send_message_params)
    assert ag_params["properties"]["Tags"]["minItems"] == 1
    assert ag_params["properties"]["Tags"]["maxItems"] == 3
    assert ag_params["properties"]["Timeout"]["minimum"] == 0
    assert ag_params["properties"]["Timeout"]["maximum"] == 30000


def test_codex_sse_error_classification_treats_invalid_schema_as_400():
    """Regression (2026-10-26): Codex SSE `response.failed` with
    `Invalid schema for function 'send_message': "1" is not of type "integer"`
    must classify as HTTP 400 (client/schema validation error), NOT 503 (which
    triggered 4x `[CodexOverloadRetry]` on the same account and tripped the
    circuit breaker as `server_error`)."""
    from app.codex_adapter import classify_codex_error_status

    err_obj = {
        "code": "invalid_function_parameters",
        "message": 'Invalid schema for function \'send_message\': "1" is not of type "integer"',
    }
    assert classify_codex_error_status(err_obj, err_obj["message"], default_status=503) == 400
    assert classify_codex_error_status({}, "Invalid schema for function 'send_message': '1' is not of type 'integer'") == 400
    assert classify_codex_error_status({"code": "rate_limit_exceeded"}, "Rate limit reached") == 429
    assert classify_codex_error_status({"code": "invalid_api_key"}, "Unauthorized token") == 401
    assert classify_codex_error_status({"code": "server_error"}, "The server had an error processing your request") == 503


def test_system_restart_endpoint_schedules_restart_and_returns_restarting(monkeypatch):
    """Verify POST /api/system/restart returns 200 {"status": "restarting"}
    and spawns the background restart thread without executing os._exit during tests."""
    from fastapi.testclient import TestClient
    import app.main as main_mod

    spawned = []

    class _DummyThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            spawned.append(self.target)

    monkeypatch.setattr(main_mod, "_is_admin_auth_enabled", lambda: False)
    monkeypatch.setattr(main_mod._threading, "Thread", _DummyThread)

    client = TestClient(main_mod.app)
    resp = client.post("/api/system/restart")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "restarting"
    assert len(spawned) == 1


if __name__ == "__main__":
    # Allow direct execution for quick local checks.
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
