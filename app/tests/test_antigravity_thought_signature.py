"""
Golden tests — Gemini 3.1-Pro thought_signature preservation (400 fix, 2026-09-04).
2026-09-05 SIG8-10: JSON-Schema 2020-12 marker strip ("Unknown name $schema"
400 fix) — envelope allowlist rewrite + ingress $-key drop.

Root cause: Gemini 3.1-Pro validates that echoed functionCall parts carry
their original thoughtSignature. The Gemini<->OpenAI converters and the SSE
translator dropped it, so the IDE's history echo was unsigned -> 400
INVALID_ARGUMENT ("Function call is missing a thought_signature").

Fix chain tested here (inline snake_case carrier between adapters):
  1. gemini.py ingress   : functionCall/functionResponse part -> tool_call
     /tool-message dict carries thought_signature
  2. antigravity_upstream: openai_to_cloudcode_envelope re-emits it as a
     camelCase SIBLING of functionCall / functionResponse
  3. models.py           : ToolCall/ToolCallFunction extra="allow" so the
     carrier survives Pydantic validation
  4. strip_thought_signature_keys: hygiene for non-antigravity lanes
  5. CloudCodeSSETranslator: upstream Gemini frames carry thoughtSignature ->
     preserved inline on the OpenAI tool_call (ROOT-CAUSE fix)
  6. gemini.py egress   : stream finish flush + non-stream twin re-emit the
     signature as camelCase sibling on the returned functionCall part

Run: .venv\\Scripts\\python -m pytest app/tests/test_antigravity_thought_signature.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.compat.adapters import (
    gemini_request_to_openai,
    openai_chunk_to_gemini,
    openai_response_to_gemini,
    unwrap_request,
)
from app.compat.adapters.gemini import _sanitize_parameters
from app.compat.adapters.antigravity_upstream import (
    CloudCodeSSETranslator,
    openai_to_cloudcode_envelope,
    strip_thought_signature_keys,
)
from app.models import ChatCompletionRequest, ToolCall, ToolCallFunction
from app.normalizer import UniversalNormalizer

SIG = "c2lnbmVkLXRvb2w="  # fake signature payload


def _wrapper(contents):
    return {
        "project": "bright-fuze-a1b2c",
        "model": "gemini-3.1-pro-high",
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": "agent-sig-1",
        "request": {"contents": contents},
    }


# ── SIG1: ingress capture ──────────────────────────────────────────────

def test_sig1_ingress_captures_signature_on_function_call_and_response():
    """Gemini functionCall/functionResponse parts carrying thoughtSignature must
    map to OpenAI carriers that keep it inline (tool_call dict / tool message)."""
    contents = [
        {"role": "user", "parts": [{"text": "go"}]},
        {"role": "model", "parts": [
            {"functionCall": {"name": "get_weather", "args": {"city": "NYC"}},
             "thoughtSignature": SIG},
        ]},
        {"role": "model", "parts": [
            {"functionResponse": {"name": "get_weather", "response": {"result": "sunny"}},
             "thoughtSignature": SIG},
        ]},
    ]
    ob = gemini_request_to_openai(unwrap_request(_wrapper(contents)), "gemini-pro-agent")

    asst = next(m for m in ob["messages"] if m.get("tool_calls"))
    assert asst["tool_calls"][0]["thought_signature"] == SIG

    tool_msg = next(m for m in ob["messages"] if m["role"] == "tool")
    assert tool_msg["thought_signature"] == SIG

    # Unsigned parts must not mint empty carriers.
    ob2 = gemini_request_to_openai(unwrap_request(_wrapper([
        {"role": "user", "parts": [{"text": "go"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "t", "args": {}}}]},
    ])), "gemini-pro-agent")
    asst2 = next(m for m in ob2["messages"] if m.get("tool_calls"))
    assert "thought_signature" not in asst2["tool_calls"][0]


# ── SIG2: envelope re-emit ─────────────────────────────────────────────

def test_sig2_envelope_reemits_signature_as_camelcase_sibling():
    """openai_to_cloudcode_envelope must re-emit thought_signature as a
    thoughtSignature SIBLING of functionCall and functionResponse parts."""
    body = {
        "model": "gemini-pro-agent",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_get_weather_1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                "thought_signature": SIG,
            }]},
            {"role": "tool", "tool_call_id": "call_get_weather_1",
             "content": '"sunny"', "thought_signature": SIG},
        ],
    }
    env = openai_to_cloudcode_envelope(body, "gemini-pro-agent")
    contents = env["request"]["contents"]

    fc_part = next(p for c in contents for p in c["parts"] if "functionCall" in p)
    assert fc_part["thoughtSignature"] == SIG
    assert fc_part["functionCall"]["name"] == "get_weather"

    fr_part = next(p for c in contents for p in c["parts"] if "functionResponse" in p)
    assert fr_part["thoughtSignature"] == SIG

    # Internal __fr__ grouping marker must never ship.
    assert all("__fr__" not in c for c in contents)


# ── SIG3: Pydantic preservation ────────────────────────────────────────

def test_sig3_pydantic_preserves_inline_signature_carrier():
    """ToolCall/ToolCallFunction extra="allow" must let the carrier survive
    validation and model_dump (the internal OpenAI round-trip)."""
    req = ChatCompletionRequest.model_validate({
        "model": "gemini-pro-agent",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_t_1", "type": "function",
                "function": {"name": "t", "arguments": "{}"},
                "thought_signature": SIG,
            }]},
            {"role": "tool", "tool_call_id": "call_t_1", "content": "ok",
             "thought_signature": SIG},
        ],
    })
    dumped = req.model_dump(exclude_none=True)
    tc = dumped["messages"][1]["tool_calls"][0]
    assert tc["thought_signature"] == SIG
    assert dumped["messages"][2]["thought_signature"] == SIG

    # Tool-level carrier too (envelope reads tc.get("thought_signature")).
    tc_obj = ToolCall.model_validate({
        "id": "x", "type": "function",
        "function": ToolCallFunction.model_validate({"name": "t", "arguments": "{}"}),
        "thought_signature": SIG,
    })
    assert tc_obj.model_dump()["thought_signature"] == SIG


# ── SIG4: hygiene strip (non-antigravity lanes) ─────────────────────────

def test_sig4_strip_removes_carriers_and_fails_open():
    """strip_thought_signature_keys removes inline carriers from tool-role
    messages and tool_calls; malformed payloads pass through untouched."""
    payload = {
        "model": "gpt-x",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "t", "arguments": "{}"},
                "thought_signature": SIG,
            }]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok",
             "thought_signature": SIG},
        ],
    }
    cleaned = strip_thought_signature_keys(payload)
    tc = cleaned["messages"][1]["tool_calls"][0]
    assert "thought_signature" not in tc
    assert tc["function"]["name"] == "t"  # rest untouched
    assert "thought_signature" not in cleaned["messages"][2]

    # Fail-open: weird shapes return the payload unchanged, never raise.
    assert strip_thought_signature_keys({}) == {}
    assert strip_thought_signature_keys({"messages": "not-a-list"}) == {"messages": "not-a-list"}
    weird = {"messages": [None, 42, {"role": "tool", "tool_calls": "nope"}]}
    assert strip_thought_signature_keys(weird) == weird


# ── SIG5: SSE translator preserves upstream signature ───────────────────

def test_sig5_translator_carries_signature_on_tool_call():
    """ROOT-CAUSE fix: a Gemini stream frame carrying thoughtSignature as a
    sibling of functionCall must surface it inline on the OpenAI tool_call —
    both on the emitted SSE chunk and in the aggregation state."""
    frame = {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [
                    {"functionCall": {"name": "grep_search", "args": {"q": "a"}},
                     "thoughtSignature": SIG},
                ],
            },
        }],
    }
    t = CloudCodeSSETranslator(model="gemini-pro-agent")
    outs = t.feed(f"data: {json.dumps(frame)}\n\n")
    assert outs, "translator must emit a chunk frame"

    chunk = json.loads(outs[0].decode("utf-8")[len("data: "):].strip())
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "grep_search"
    assert tc["thought_signature"] == SIG

    # Aggregation state (feeds non-stream client paths) keeps it too.
    assert t.agg["tool_calls"][0]["thought_signature"] == SIG

    # Unsigned frames must not mint empty carriers.
    t2 = CloudCodeSSETranslator(model="m")
    frame2 = {
        "candidates": [{
            "content": {"role": "model", "parts": [
                {"functionCall": {"name": "t", "args": {}}},
            ]},
        }],
    }
    outs2 = t2.feed(f"data: {json.dumps(frame2)}\n\n")
    chunk2 = json.loads(outs2[0].decode("utf-8")[len("data: "):].strip())
    assert "thought_signature" not in chunk2["choices"][0]["delta"]["tool_calls"][0]


# ── SIG6: stream egress finish flush re-emit ────────────────────────────

def test_sig6_stream_finish_flush_reemits_signature_sibling():
    """A tool_call delta carrying thought_signature accumulates into the slot
    and the finish flush re-emits it as a camelCase sibling of functionCall."""
    state = {}
    chunks = [
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": ""},
             "thought_signature": SIG},
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"city":"NYC"}'}},
        ]}, "finish_reason": None}]},
        {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {},
                                              "finish_reason": "tool_calls"}]},
    ]
    emitted = [openai_chunk_to_gemini(c, state) for c in chunks]
    assert emitted[0] is None and emitted[1] is None

    part = emitted[2]["response"]["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"]["name"] == "get_weather"
    assert part["thoughtSignature"] == SIG


# ── SIG7: non-stream twin re-emit ──────────────────────────────────────

def test_sig7_non_stream_twin_reemits_signature_sibling():
    """openai_response_to_gemini must re-emit the inline carrier as a
    thoughtSignature sibling on the returned functionCall part."""
    openai_resp = {
        "id": "chatcmpl-9",
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                    "thought_signature": SIG,
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    out = openai_response_to_gemini(openai_resp, "m")
    parts = out["response"]["candidates"][0]["content"]["parts"]
    fc_part = next(p for p in parts if "functionCall" in p)
    assert fc_part["functionCall"]["name"] == "get_weather"
    assert fc_part["thoughtSignature"] == SIG


# ── SIG8-10: JSON-Schema 2020-12 marker strip ("Unknown name $schema" 400) ──

KITCHEN_SINK = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:bsl:edit",
    "$defs": {"Foo": {"type": "object"}},
    "definitions": {"legacy": {"type": "string"}},
    "title": "EditArgs",
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path"},
        "mode": {"type": "string", "enum": ["edit", "replace"]},
        "count": {"type": "INTEGER", "minimum": 1, "maximum": 99,
                  "exclusiveMinimum": 0, "exclusiveMaximum": 100},
        "tags": {"type": "ARRAY", "items": {"type": "string"},
                 "minItems": 1, "maxItems": 9},
        "when": {"type": "string", "format": "date-time"},
        "meta": {"type": "object", "additionalProperties": {"type": "string"},
                 "default": {}, "examples": [{"a": "b"}]},
    },
    "required": ["path"],
    "additionalProperties": False,
    "default": {},
    "examples": [{"path": "x"}],
}


def _envelope_params(params):
    body = {
        "model": "gemini-pro-agent",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {
            "name": "edit_file", "description": "edit", "parameters": params,
        }}],
    }
    env = openai_to_cloudcode_envelope(body, "gemini-pro-agent")
    decls = env["request"]["tools"][0]["functionDeclarations"]
    return decls[0]["parameters"]


def _assert_no_stripped_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            assert not k.startswith("$"), k
            assert k not in (
                "definitions", "title", "examples", "default",
                "additionalProperties", "exclusiveMinimum", "exclusiveMaximum",
                "oneOf", "allOf", "patternProperties", "if", "then", "else",
            ), k
            _assert_no_stripped_keys(v)
    elif isinstance(node, list):
        for item in node:
            _assert_no_stripped_keys(item)


def test_sig8_envelope_strips_2020_12_markers_and_never_mutates_input():
    """Kitchen-sink 2020-12 parameters must lose every proto-illegal key at
    every depth, keep proto-valid constraint fields, and leave the caller's
    dict untouched."""
    import copy
    snapshot = copy.deepcopy(KITCHEN_SINK)
    out = _envelope_params(KITCHEN_SINK)

    _assert_no_stripped_keys(out)
    assert out["type"] == "object"
    assert out["required"] == ["path"]
    props = out["properties"]
    assert props["count"]["type"] == "integer"          # INTEGER lowercased
    assert props["count"]["minimum"] == 1 and props["count"]["maximum"] == 99
    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"] == {"type": "string"}
    assert props["tags"]["minItems"] == 1 and props["tags"]["maxItems"] == 9
    assert props["mode"]["enum"] == ["edit", "replace"]
    assert props["when"]["format"] == "date-time"
    assert props["meta"]["type"] == "object"
    assert KITCHEN_SINK == snapshot                    # input never mutated


def test_sig9_envelope_flattens_type_arrays_and_renames_oneof():
    """Draft-07 unions have no proto equivalent: type arrays flatten to a
    scalar (+nullable when 'null' present) and oneOf renames to anyOf — the
    union Gemini actually supports."""
    params = {
        "type": "object",
        "properties": {
            "maybe": {"type": ["STRING", "null"]},
            "pick": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "plain": {"type": "string"},
        },
    }
    out = _envelope_params(params)
    props = out["properties"]
    assert props["maybe"] == {"type": "string", "nullable": True}
    assert props["pick"]["anyOf"] == [{"type": "string"}, {"type": "integer"}]
    assert "oneOf" not in props["pick"]
    assert props["plain"] == {"type": "string"}


def test_sig11_envelope_emits_only_allowlisted_keys():
    """Governance pin (2026-09-05 cleanup): every key the egress envelope
    emits inside functionDeclarations[].parameters must be a member of
    _GEMINI_SCHEMA_KEEP (plus the synthesized ``nullable`` from type-array
    flattening). Widening the allowlist without live-upstream evidence now
    fails this test loudly; a field proven live goes into _GEMINI_SCHEMA_KEEP
    and is covered here automatically.

    Probe evidence recorded 2026-09-05: the raw CloudCode endpoint
    accepts-and-ignores unknown fields (``$schema`` at parameters top-level
    returned 200 post-envelope), so ONLY the IDE-lane validated tool-mode
    400s — meaning out-of-band probes can never justify widening. The
    allowlist stays conservative until an in-lane probe proves a field.
    """
    from app.compat.adapters.antigravity_upstream import _GEMINI_SCHEMA_KEEP
    legal = _GEMINI_SCHEMA_KEEP | {"nullable"}
    out = _envelope_params(KITCHEN_SINK)

    def _check(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    # Map values carry ARBITRARY user-defined property NAMES —
                    # only the subschema FIELDS are pinned, never the names
                    # (mirrors the normalizer's own exemption).
                    for sub in v.values():
                        _check(sub)
                    continue
                assert k in legal, f"emitted non-allowlisted key {k!r}"
                _check(v)
        elif isinstance(node, list):
            for item in node:
                _check(item)

    _check(out)


def test_sig10_ingress_keeps_json_schema_markers():
    """Layering pin: ingress targets OpenAI consumers (which accept full
    JSON-Schema — see 5b1 T2), so $-markers/definitions SURVIVE with nested
    types lowercased and enumDescriptions dropped; the proto strip lives
    exclusively in the egress envelope builder (SIG8/SIG9)."""
    params = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"Foo": {"type": "OBJECT", "properties": {"ok": {"type": "Boolean"}}}},
        "definitions": {"legacy": {"type": "String"}},
        "type": "object",
        "properties": {
            "mode": {"type": "STRING", "enum": ["a"], "enumDescriptions": ["A"]},
            "maybe": {"type": ["string", "null"], "title": "kept"},
        },
        "required": ["mode"],
    }
    out = _sanitize_parameters(params)
    assert out["$schema"].startswith("https://json-schema.org")       # kept
    assert out["$defs"]["Foo"]["properties"]["ok"]["type"] == "boolean"  # T2
    assert out["definitions"]["legacy"]["type"] == "string"          # kept
    mode = out["properties"]["mode"]
    assert mode["type"] == "string" and mode["enum"] == ["a"]
    assert "enumDescriptions" not in mode                            # hygiene
    assert out["properties"]["maybe"]["type"] == ["string", "null"]  # draft-07
    assert out["properties"]["maybe"]["title"] == "kept"


# ── SIG12: client=anthropic round-trip preservation (2026-09-17) ─────────────
# Anthropic ingress (normalize_to_openai_from_anthropic) carries the signature
# inline on the OpenAI tool_call dict; extra="allow" on ToolCall keeps it
# through Pydantic validation; egress (normalize_to_anthropic) re-emits it on
# the rebuilt tool_use block. This is the chain Claude Code drives.

def test_sig12_anthropic_wire_signature_survives_openai_roundtrip():
    body = {
        "model": "claude-opus-5",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "NYC"}, "thought_signature": SIG},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny"},
            ]},
        ],
    }
    # Ingress: anthropic -> openai body (dict level).
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(body)
    asst = next(m for m in openai_body["messages"] if m.get("tool_calls"))
    assert asst["tool_calls"][0]["thought_signature"] == SIG

    # Internal round-trip: Pydantic validation must keep the carrier.
    req = ChatCompletionRequest.model_validate(openai_body)
    # Egress: openai -> anthropic payload re-emits on tool_use block.
    anth = UniversalNormalizer.normalize_to_anthropic(req)
    asst2 = next(m for m in anth["messages"] if m["role"] == "assistant")
    tu = next(b for b in asst2["content"] if b.get("type") == "tool_use")
    assert tu["id"] == "toolu_1"
    assert tu["thought_signature"] == SIG


def test_sig12b_unsigned_tool_use_mints_no_carrier():
    """Unsigned tool_use blocks must not mint empty carriers at any stage."""
    body = {
        "model": "claude-opus-5",
        "max_tokens": 64,
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_2", "name": "t", "input": {}},
            ]},
        ],
    }
    openai_body = UniversalNormalizer.normalize_to_openai_from_anthropic(body)
    tc = next(m for m in openai_body["messages"] if m.get("tool_calls"))["tool_calls"][0]
    assert "thought_signature" not in tc

    req = ChatCompletionRequest.model_validate(openai_body)
    anth = UniversalNormalizer.normalize_to_anthropic(req)
    tu = next(b for m in anth["messages"] for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_use")
    assert "thought_signature" not in tu


# ── SIG13: anthropic-wire strip hygiene (2026-09-17) ─────────────────────────

def test_sig13_strip_covers_anthropic_tool_use_blocks():
    """After normalize_to_anthropic re-emit, the carrier sits on tool_use
    content blocks; the hygiene gate must strip it there for non-antigravity
    anthropic lanes (GLM etc.), fail-open as before."""
    payload = {
        "model": "glm-x",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "NYC"}, "thought_signature": SIG},
            ]},
        ],
    }
    cleaned = strip_thought_signature_keys(payload)
    tu = next(b for m in cleaned["messages"] for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_use")
    assert "thought_signature" not in tu
    assert tu["name"] == "get_weather"  # rest untouched
    # camelCase variant also stripped (Gemini-style ingress on anthropic wire).
    payload2 = {
        "messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "x", "name": "t", "input": {},
             "thoughtSignature": SIG},
        ]}],
    }
    cleaned2 = strip_thought_signature_keys(payload2)
    tu2 = cleaned2["messages"][0]["content"][0]
    assert "thoughtSignature" not in tu2
