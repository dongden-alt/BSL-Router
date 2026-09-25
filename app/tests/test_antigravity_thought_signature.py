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


# ── SIG14: STREAMING Anthropic emit (Step 1, 2026-09-21) ──────────────────────
# The 2026-09-04 SIG1-13 suite covered only the NON-streaming paths
# (UniversalNormalizer + gemini.py adapters). These cover the streaming
# OpenAI->Anthropic emit (SIG-EMIT in _tool_events) and the streaming
# gemini->OpenAI capture, which is the path Antigravity IDE actually drives.

import asyncio
from app.compat.stream_normalizer import StreamNormalizer


def _oenc(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _ochunk(delta=None, finish=None):
    return {
        "id": "chatcmpl-test", "object": "chat.completion.chunk",
        "created": 0, "model": "test-model",
        "choices": [{"index": 0, "delta": {} if delta is None else delta,
                     "finish_reason": finish}],
    }


async def _byte_stream(frames):
    for f in frames:
        yield f


async def _collect(stream):
    out = b""
    async for chunk in stream:
        out += chunk
    return out


def _anthropic_events(raw):
    events, etype = [], None
    for line in raw.split("\n"):
        if line.startswith("event: "):
            etype = line[7:].strip()
        elif line.startswith("data: "):
            events.append((etype, json.loads(line[6:])))
            etype = None
    return events


def _openai_chunks(raw):
    return [json.loads(line[6:]) for line in raw.split("\n")
            if line.startswith("data: ") and line[6:].strip() != "[DONE]"]


def test_sig14_stream_tool_call_delta_carries_signature_to_anthropic():
    """An OpenAI stream tool_call delta carrying thought_signature must surface
    it on the emitted Anthropic tool_use content_block_start (SIG-EMIT)."""
    frames = [
        _oenc(_ochunk(delta={"role": "assistant", "content": ""})),
        _oenc(_ochunk(delta={"tool_calls": [{
            "index": 0, "id": "call_read_1", "type": "function",
            "function": {"name": "read", "arguments": ""},
            "thought_signature": "SIGSTREAM"}]})),
        _oenc(_ochunk(delta={"tool_calls": [{
            "index": 0, "function": {"arguments": '{"path":"a"}'}}]})),
        _oenc(_ochunk(delta={}, finish="tool_calls")),
    ]
    n = StreamNormalizer("openai_sse", "anthropic_sse", model_name="m")
    raw = asyncio.run(_collect(
        n.convert_openai_to_anthropic(_byte_stream(frames)))).decode("utf-8")
    events = _anthropic_events(raw)

    tool_starts = [d for etype, d in events if etype == "content_block_start"
                   and d.get("content_block", {}).get("type") == "tool_use"]
    assert tool_starts, "tool_use content_block_start must be emitted"
    cb = tool_starts[0]["content_block"]
    assert cb["name"] == "read"
    assert cb["thought_signature"] == "SIGSTREAM"

    md = [d for etype, d in events if etype == "message_delta"]
    assert md and md[-1]["delta"]["stop_reason"] == "tool_use"


def test_sig14b_stream_unsigned_tool_call_emits_no_signature_key():
    """Unsigned stream tool_call must NOT mint a thought_signature key (wire
    hygiene: absence is the only valid state for a missing signature)."""
    frames = [
        _oenc(_ochunk(delta={"tool_calls": [{
            "index": 0, "id": "call_read_2", "type": "function",
            "function": {"name": "read", "arguments": '{"p":1}'}}]})),
        _oenc(_ochunk(delta={}, finish="tool_calls")),
    ]
    n = StreamNormalizer("openai_sse", "anthropic_sse", model_name="m")
    raw = asyncio.run(_collect(
        n.convert_openai_to_anthropic(_byte_stream(frames)))).decode("utf-8")
    cb = next(d["content_block"] for etype, d in _anthropic_events(raw)
              if etype == "content_block_start"
              and d.get("content_block", {}).get("type") == "tool_use")
    assert "thought_signature" not in cb


def test_sig14c_stream_split_tool_call_signature_survives_reassembly():
    """Signature arriving on the FIRST delta of a multi-fragment tool_call must
    survive reassembly into the final tool_use block."""
    frames = [
        _oenc(_ochunk(delta={"tool_calls": [{
            "index": 0, "id": "call_edit_1", "type": "function",
            "function": {"name": "edit", "arguments": ""},
            "thought_signature": "SPLIT"}]})),
        _oenc(_ochunk(delta={"tool_calls": [
            {"index": 0, "function": {"arguments": '{"a":'}}]})),
        _oenc(_ochunk(delta={"tool_calls": [
            {"index": 0, "function": {"arguments": '1}'}}]})),
        _oenc(_ochunk(delta={}, finish="tool_calls")),
    ]
    n = StreamNormalizer("openai_sse", "anthropic_sse", model_name="m")
    raw = asyncio.run(_collect(
        n.convert_openai_to_anthropic(_byte_stream(frames)))).decode("utf-8")
    cb = next(d["content_block"] for etype, d in _anthropic_events(raw)
              if etype == "content_block_start"
              and d.get("content_block", {}).get("type") == "tool_use")
    assert cb["thought_signature"] == "SPLIT"


# ── SIG15: nested capture + router-side cache (Steps 2 & 3, 2026-09-21) ───────

def test_sig15_nested_thoughtsignature_captured_by_translator():
    """Step 2: thoughtSignature nested INSIDE functionCall (not a part sibling)
    must still be captured onto the OpenAI tool_call carrier."""
    frame = {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "grep_search", "args": {"q": "a"},
                          "thoughtSignature": SIG}},
    ]}}]}
    t = CloudCodeSSETranslator(model="gemini-pro-agent")
    outs = t.feed(f"data: {json.dumps(frame)}\n\n")
    assert outs
    chunk = json.loads(outs[0].decode("utf-8")[len("data: "):].strip())
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["thought_signature"] == SIG
    assert t.agg["tool_calls"][0]["thought_signature"] == SIG


def test_sig15b_gemini_stream_both_nesting_levels_to_openai():
    """convert_gemini_to_openai captures sibling-level AND nested-level
    thoughtSignature onto the emitted OpenAI tool_call deltas."""
    frame = {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "thinking", "thought": True},
        {"functionCall": {"name": "a", "args": {}}, "thoughtSignature": "SIB"},
        {"functionCall": {"name": "b", "args": {}, "thoughtSignature": "NEST"}},
    ]}, "finishReason": "STOP"}]}
    n = StreamNormalizer("gemini_sse", "openai_sse", model_name="m")
    frames = [f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8")]
    raw = asyncio.run(_collect(
        n.convert_gemini_to_openai(_byte_stream(frames)))).decode("utf-8")
    tool_deltas = [c for c in _openai_chunks(raw)
                   if c["choices"][0]["delta"].get("tool_calls")]
    sigs = {tc["function"]["name"]: tc.get("thought_signature")
            for c in tool_deltas for tc in c["choices"][0]["delta"]["tool_calls"]}
    assert sigs["a"] == "SIB"
    assert sigs["b"] == "NEST"


def test_sig15g_stream_normalizer_stores_signature_in_shared_cache():
    """The Anthropic client path (stream_normalizer convert_gemini_to_openai)
    must store the captured thoughtSignature into the shared router-side cache,
    so the envelope converter can re-inject it on the next-turn echo even when
    the client drops the carrier. Regression test for the BSC gemini-pro-agent
    400 where the cache was empty because this path never stored."""
    import app.compat.adapters.antigravity_upstream as ag
    ag._SIGNATURE_CACHE.clear()

    frame = {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "read", "args": {"path": "README.md"}},
         "thoughtSignature": SIG},
    ]}, "finishReason": "STOP"}]}
    n = StreamNormalizer("gemini_sse", "openai_sse", model_name="gemini-pro-agent")
    frames = [f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8")]
    raw = asyncio.run(_collect(
        n.convert_gemini_to_openai(_byte_stream(frames)))).decode("utf-8")

    # The emitted chunk must carry the signature inline.
    tool_deltas = [c for c in _openai_chunks(raw)
                   if c["choices"][0]["delta"].get("tool_calls")]
    assert tool_deltas, "expected at least one tool_call delta"
    tc = tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["thought_signature"] == SIG

    # The shared cache must have been populated by the stream_normalizer.
    # Key: (model_name, minted_id, tool_name, args-digest).
    minted_id = tc["id"]
    key = ag._signature_cache_key("gemini-pro-agent", minted_id, "read",
                                  {"path": "README.md"})
    assert ag._signature_cache_lookup(key) == SIG, (
        "stream_normalizer must store the signature in the shared cache "
        "so the envelope converter can find it on turn-2 echo"
    )

    # The args-digest fallback must also find it (name/id-agnostic).
    assert ag._signature_cache_lookup_fallback(
        "gemini-pro-agent", {"path": "README.md"}) == SIG

    ag._SIGNATURE_CACHE.clear()


def test_sig15c_signature_cache_store_lookup_and_eviction():
    """Step 3a/3b: composite key is stable across dict/str arg forms; the LRU
    evicts the oldest entry beyond the 512 cap."""
    import app.compat.adapters.antigravity_upstream as ag
    ag._SIGNATURE_CACHE.clear()

    # Same logical call expressed as dict (store side) and JSON str (lookup side)
    k_dict = ag._signature_cache_key("m", "call_read_1", "read", {"path": "a"})
    k_str = ag._signature_cache_key("m", "call_read_1", "read", '{"path":"a"}')
    assert k_dict == k_str, "dict and str arg forms must hash identically"

    # Different args => different key (no id-only collision).
    k_other = ag._signature_cache_key("m", "call_read_1", "read", {"path": "b"})
    assert k_other != k_dict

    ag._signature_cache_store(k_dict, "S1")
    assert ag._signature_cache_lookup(k_dict) == "S1"
    assert ag._signature_cache_lookup(k_other) is None  # silent miss

    # Eviction: overflow past the cap drops the oldest.
    for i in range(ag._SIGNATURE_CACHE_MAX + 1):
        ag._signature_cache_store(("m", f"c{i}", "n", f"{i}"), f"S{i}")
    assert len(ag._SIGNATURE_CACHE) <= ag._SIGNATURE_CACHE_MAX
    ag._SIGNATURE_CACHE.clear()


def test_sig15d_signature_cache_ttl_expiry(monkeypatch):
    """Step 3b: an entry older than the TTL is dropped and counted as a miss."""
    import time as _t
    import app.compat.adapters.antigravity_upstream as ag
    ag._SIGNATURE_CACHE.clear()
    monkeypatch.setattr(ag, "_SIGNATURE_CACHE_TTL", 0.01)
    k = ag._signature_cache_key("m", "c", "n", {})
    ag._signature_cache_store(k, "S")
    assert ag._signature_cache_lookup(k) == "S"
    _t.sleep(0.02)
    assert ag._signature_cache_lookup(k) is None
    ag._SIGNATURE_CACHE.clear()


def test_sig15e_envelope_reinjects_signature_from_cache_when_client_drops_it():
    """Step 3d: when the echoed tool_call has NO inline thought_signature, the
    envelope builder must re-inject it from the router-side cache (keyed by
    model+id+name+args) so the next-turn functionCall part stays signed."""
    import app.compat.adapters.antigravity_upstream as ag
    ag._SIGNATURE_CACHE.clear()
    ag.SIGNATURE_CACHE_HITS = 0
    ag.SIGNATURE_CACHE_MISSES = 0

    key = ag._signature_cache_key("gemini-pro-agent", "call_read_1", "read",
                                  '{"path":"a"}')
    ag._signature_cache_store(key, "CACHED_SIG")

    # Echo body WITHOUT the inline carrier (client dropped the unknown field).
    body = {
        "model": "gemini-pro-agent",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_read_1", "type": "function",
                "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
            {"role": "tool", "tool_call_id": "call_read_1", "content": '"data"'},
        ],
    }
    env = openai_to_cloudcode_envelope(body, "gemini-pro-agent")
    fc_part = next(p for c in env["request"]["contents"]
                   for p in c["parts"] if "functionCall" in p)
    assert fc_part["thoughtSignature"] == "CACHED_SIG"
    assert ag.SIGNATURE_CACHE_HITS >= 1
    ag._SIGNATURE_CACHE.clear()


def test_sig15f_cache_counters_track_hits_and_misses():
    """Observability is two integer counters (no per-entry logging, AGENTS.md
    §2); both must advance correctly."""
    import app.compat.adapters.antigravity_upstream as ag
    ag._SIGNATURE_CACHE.clear()
    ag.SIGNATURE_CACHE_HITS = 0
    ag.SIGNATURE_CACHE_MISSES = 0
    k = ag._signature_cache_key("m", "c1", "n", {})
    ag._signature_cache_store(k, "S")
    assert ag.SIGNATURE_CACHE_MISSES == 0  # store does not look up
    ag._signature_cache_lookup(k)                       # hit
    ag._signature_cache_lookup(("m", "absent", "n", ""))  # miss
    assert ag.SIGNATURE_CACHE_HITS == 1
    assert ag.SIGNATURE_CACHE_MISSES == 1
    ag._SIGNATURE_CACHE.clear()


# ── Step 4 (normalizer_v2 egress sibling-attach) — REVERTED 2026-09-22 ──────
# A first attempt rewrote _egress_gemini to attach thoughtSignature as a sibling
# key on the functionCall Part instead of appending a standalone
# {"thoughtSignature": ...} Part (the plan called the standalone form a latent
# wire-contract bug at Q4). It was reverted because:
#   1. normalizer_v2 is SHADOW-ONLY (normalizer_shadow imports it read-only), so
#      the change delivered zero benefit to the live antigravity 400 — that is
#      fixed by Steps 1-3 + the stream_normalizer emit path, all covered above
#      (SIG14/SIG15) and by openai_to_cloudcode_envelope (SIG15e).
#   2. The sibling egress made v2 INTERNALLY ASYMMETRIC: _egress_gemini emitted a
#      sibling key, but _ingress_gemini (the standalone-sig collapse) only reads
#      the standalone form, so a gemini->canonical->gemini round-trip no longer
#      re-canonicalized clean — breaking
#      test_normalizer_shadow.py::test_gemini_thought_signature_roundtrip_compares_clean.
#   3. Plan Q4 ("fix now or file separately to keep this change tight") was never
#      explicitly answered; reverting is the conservative, baseline-restoring call.
# DEFERRED (separate scoped task, NOT this incident): if v2 is ever promoted off
# shadow, fix the wire contract end-to-end in ONE pass — egress sibling-attach +
# ingress sibling-read + the shadow comparator's attach/detach invariant — and
# land it with its own regression tests.


# ── SIG11-14: cache-miss hardening (2026-09-25) ─────────────────────────────
# The 400 recurred on antigravity/gemini-3.6-flash-high ("default_api:invalid",
# position 20) because the egress re-injection silently emitted an UNSIGNED
# functionCall on a router-side cache MISS. The primary key embeds the volatile
# per-turn call_id and the tool name (placeholder names break it), and the TTL
# was 900s monotonic. These tests lock the fix: name/id-agnostic args-digest
# fallback, 3600s wall-clock TTL, and a never-crash unsigned-emit warn.

from app.compat.adapters import antigravity_upstream as _agu


def _reset_sig_cache():
    with _agu._SIGNATURE_CACHE_LOCK:
        _agu._SIGNATURE_CACHE.clear()
    _agu.SIGNATURE_CACHE_HITS = 0
    _agu.SIGNATURE_CACHE_MISSES = 0
    _agu.SIGNATURE_CACHE_FALLBACK_HITS = 0
    _agu._UNSIGNED_WARNED = False


def _unsigned_body(name, call_id, args_json):
    """Assistant tool_call WITHOUT the inline thought_signature carrier, so the
    envelope must rely on the router-side cache to re-sign it."""
    return {
        "model": "gemini-pro-agent",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": args_json},
            }]},
        ],
    }


def test_sig11_fallback_reinjects_when_call_id_differs():
    """A stored signature must be re-injected on the echo even when the echoed
    call_id differs (per-turn id churn) -- via the (model, args_digest) fallback."""
    _reset_sig_cache()
    model = "gemini-pro-agent"
    args = {"city": "NYC"}
    # Store side keyed by the ORIGINAL turn's id.
    _agu._signature_cache_store(
        _agu._signature_cache_key(model, "call_get_weather_1", "get_weather", args), SIG
    )
    # Echo arrives with a DIFFERENT id (client re-minted) and no inline carrier.
    body = _unsigned_body("get_weather", "call_get_weather_7", '{"city":"NYC"}')
    env = openai_to_cloudcode_envelope(body, model)
    fc = next(p for c in env["request"]["contents"] for p in c["parts"] if "functionCall" in p)
    assert fc["thoughtSignature"] == SIG
    assert _agu.SIGNATURE_CACHE_FALLBACK_HITS == 1


def test_sig12_placeholder_name_resolved_via_args_digest():
    """The 'default_api:invalid' placeholder name must still re-sign: the
    fallback ignores name entirely and matches on (model, args_digest)."""
    _reset_sig_cache()
    model = "gemini-3.6-flash-high"
    args = {"path": "README.md"}
    _agu._signature_cache_store(
        _agu._signature_cache_key(model, "call_read_1", "read", args), SIG
    )
    # Client echo uses a placeholder name + a fresh id, no inline carrier.
    body = _unsigned_body("default_api:invalid", "call_default_api:invalid_3",
                          '{"path":"README.md"}')
    env = openai_to_cloudcode_envelope(body, model)
    fc = next(p for c in env["request"]["contents"] for p in c["parts"] if "functionCall" in p)
    assert fc["thoughtSignature"] == SIG
    assert _agu.SIGNATURE_CACHE_FALLBACK_HITS == 1


def test_sig13_expired_entry_not_used_fresh_entry_is():
    """An entry older than the TTL must NOT be used; a fresh one must."""
    import time as _t
    _reset_sig_cache()
    model = "gemini-pro-agent"
    args = {"q": "x"}
    key = _agu._signature_cache_key(model, "call_s_1", "search", args)
    # Manually backdate the entry beyond the TTL.
    with _agu._SIGNATURE_CACHE_LOCK:
        _agu._SIGNATURE_CACHE[key] = (SIG, _t.time() - (_agu._SIGNATURE_CACHE_TTL + 10))
    assert _agu._signature_cache_lookup_fallback(model, args) is None
    # A fresh entry on the same digest IS used.
    _agu._signature_cache_store(key, SIG)
    assert _agu._signature_cache_lookup_fallback(model, args) == SIG


def test_sig14_unsigned_emit_never_raises_and_warns_once(capsys):
    """When no signature is available anywhere, the part still ships (never
    crash) and exactly one throttled diagnostic is emitted."""
    _reset_sig_cache()
    model = "gemini-pro-agent"
    body = _unsigned_body("no_such_tool", "call_no_such_tool_1", '{"a":1}')
    env = openai_to_cloudcode_envelope(body, model)
    fc = next(p for c in env["request"]["contents"] for p in c["parts"] if "functionCall" in p)
    assert "thoughtSignature" not in fc  # unsigned, but emitted (no exception)
    out = capsys.readouterr().out
    assert "thought_signature cache miss" in out
    # Second unsigned emit must NOT warn again (throttled once per process).
    _agu._warn_unsigned_once(model, "another")
    out2 = capsys.readouterr().out
    assert out2 == ""

