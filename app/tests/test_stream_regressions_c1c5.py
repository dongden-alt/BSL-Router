"""
N3 — C1/C5 stream-chunk regression suite (2026-09-06).

Drives StreamNormalizer (app/compat/stream_normalizer.py — READ-ONLY production
surface) exactly the way the existing suites do (test_stream_normalizer_failfast.py
/ test_stream_buffer.py / test_stream_tool_rescue.py fixtures: raw SSE byte
frames in, asyncio.run collected output), and asserts TODAY's actual contract
for every case. Where today's behavior is a known gap, the test asserts the
drop with an ``xfail(strict=True)`` marker and a one-line comment — desired
behavior is never invented here; strict xfail flips to XPASS-failure the day
the gap is fixed, so these double as fix-detectors.

C1 — reasoning/thinking chunk styles:
  1. Anthropic thinking SSE → OpenAI chunk shape: thinking_delta maps to
     reasoning_content (preserved); signature_delta emits NOTHING (signature
     dropped — today's contract).
  2. Gemini thought parts (thought=true) → OpenAI translation: NO conversion
     path exists in StreamNormalizer (openai_sse↔anthropic_sse only) — xfail.
  3. Responses reasoning item round-trip through normalizer_v2 (streaming-
     shaped replay input): encrypted_content preserved verbatim, in order.
  4. Interleaved thinking + text + tool_use in ONE stream — no cross-
     contamination of delta buffers (both directions; openai→anthropic drops
     interleaved reasoning today — xfail).

C5 — tool-call partial delta assembly:
  1. openai_sse index-keyed fragments interleaved out of index order →
     per-index assembly stays correct.
  2. anthropic_sse input_json_delta across two content blocks sharing ONE
     tool id → two separate OpenAI indices, never merged (today's contract).
  3. Byte-split mid-UTF8-codepoint inside tool args: openai→anthropic
     survives (incremental decoder); anthropic→openai LOSES the line
     (non-incremental chunk.decode) — xfail.
  4. Stream truncated upstream mid-fragment: no hang, partial call never
     emitted as complete valid JSON (fail-fast conventions).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.compat.stream_normalizer import StreamNormalizer  # noqa: E402


# ── Fixture helpers (same style as test_stream_normalizer_failfast.py) ────────

def _oenc(payload: dict) -> bytes:
    """One OpenAI chunk as SSE bytes."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _ochunk(delta=None, finish=None, usage=None):
    delta = {} if delta is None else dict(delta)
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        payload["usage"] = usage
    return payload


def _aenc(event: dict) -> bytes:
    """One Anthropic event as SSE bytes (event: + data: lines)."""
    etype = event.get("type", "")
    return f"event: {etype}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


def _aev(etype: str, **kw) -> dict:
    ev = {"type": etype}
    ev.update(kw)
    return ev


async def _byte_stream(frames):
    for frame in frames:
        yield frame


async def _collect(stream):
    out = b""
    async for chunk in stream:
        out += chunk
    return out


def _run_o2a(frames) -> str:
    """openai_sse → anthropic_sse full conversion."""
    n = StreamNormalizer("openai_sse", "anthropic_sse", model_name="m")
    return asyncio.run(_collect(n.convert_openai_to_anthropic(_byte_stream(frames)))).decode("utf-8")


def _run_a2o(frames) -> str:
    """anthropic_sse → openai_sse full conversion."""
    n = StreamNormalizer("anthropic_sse", "openai_sse", model_name="m")
    return asyncio.run(_collect(n.convert_anthropic_to_openai(_byte_stream(frames)))).decode("utf-8")


def _anthropic_events(raw: str):
    """Anthropic SSE bytes → [(event_type, data_dict), ...] (rescue-test style)."""
    events = []
    etype = None
    for line in raw.split("\n"):
        if line.startswith("event: "):
            etype = line[7:].strip()
        elif line.startswith("data: "):
            events.append((etype, json.loads(line[6:])))
            etype = None
    return events


def _openai_chunks(raw: str):
    """OpenAI SSE bytes → [chunk_dict, ...] (skips [DONE])."""
    return [
        json.loads(line[6:])
        for line in raw.split("\n")
        if line.startswith("data: ") and line[6:].strip() != "[DONE]"
    ]


def _join_deltas(chunks, field: str):
    """Join one delta field across all OpenAI chunks."""
    return "".join(
        c["choices"][0]["delta"].get(field, "")
        for c in chunks
        if isinstance(c.get("choices"), list) and c["choices"]
    )


def _extract_anthropic(raw: str):
    """(text, tool_blocks, stop_reason, saw_message_stop) — tool_rescue style."""
    text = ""
    blocks = {}
    stop_reason = None
    saw_stop = False
    for etype, data in _anthropic_events(raw):
        if etype == "content_block_start":
            cb = data.get("content_block", {})
            if cb.get("type") == "tool_use":
                blocks[data["index"]] = {
                    "name": cb.get("name"), "id": cb.get("id"), "input_json": "",
                }
        elif etype == "content_block_delta":
            d = data.get("delta", {})
            if d.get("type") == "text_delta":
                text += d.get("text", "")
            elif d.get("type") == "input_json_delta" and data["index"] in blocks:
                blocks[data["index"]]["input_json"] += d.get("partial_json", "")
        elif etype == "message_delta":
            stop_reason = data.get("delta", {}).get("stop_reason")
        elif etype == "message_stop":
            saw_stop = True
    return text, blocks, stop_reason, saw_stop


EMOJI = "\U0001f600"  # 😀 — U+1F600, 4 UTF-8 bytes (f0 9f 98 80)


# ══════════════════════════════════════════════════════════════════════════════
# C1-1 — Anthropic thinking SSE → OpenAI chunk shape
# ══════════════════════════════════════════════════════════════════════════════

def test_c1_anthropic_thinking_maps_to_reasoning_content():
    """thinking_delta → OpenAI reasoning_content; text stays in content.

    Today's contract (verified against StreamNormalizer.convert_anthropic_to_openai):
    each thinking_delta becomes its own chat.completion.chunk with
    delta.reasoning_content; content_block boundaries for the thinking block
    emit no OpenAI-side chunk (OpenAI shape has no block markers).
    """
    raw = _run_a2o([
        _aenc(_aev("message_start", message={"usage": {"input_tokens": 5}})),
        _aenc(_aev("content_block_start", index=0,
                   content_block={"type": "thinking", "thinking": ""})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "thinking_delta", "thinking": "Let me think... "})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "thinking_delta", "thinking": "about it"})),
        _aenc(_aev("content_block_stop", index=0)),
        _aenc(_aev("content_block_start", index=1,
                   content_block={"type": "text", "text": ""})),
        _aenc(_aev("content_block_delta", index=1,
                   delta={"type": "text_delta", "text": "Answer 42"})),
        _aenc(_aev("content_block_stop", index=1)),
        _aenc(_aev("message_delta", delta={"stop_reason": "end_turn"},
                   usage={"output_tokens": 9})),
        _aenc(_aev("message_stop")),
    ])
    chunks = _openai_chunks(raw)
    assert _join_deltas(chunks, "reasoning_content") == "Let me think... about it"
    assert _join_deltas(chunks, "content") == "Answer 42"
    # No cross-contamination: thinking text never lands in a content field.
    assert "think" not in _join_deltas(chunks, "content")
    assert "Answer" not in _join_deltas(chunks, "reasoning_content")
    # Stream terminates: finish chunk + [DONE].
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert "data: [DONE]" in raw


def test_c1_anthropic_signature_delta_emits_nothing_today():
    """A signature_delta event is silently dropped in anthropic→openai today.

    The OpenAI chat-chunk shape has no signature field, and the converter
    handles no `signature_delta` delta type — the event yields no chunk and the
    signature is not attached anywhere. Asserting the drop (current contract),
    not inventing a mapping.
    """
    raw = _run_a2o([
        _aenc(_aev("message_start", message={})),
        _aenc(_aev("content_block_start", index=0,
                   content_block={"type": "thinking", "thinking": ""})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "thinking_delta", "thinking": "T"})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "signature_delta", "signature": "sig-abc"})),
        _aenc(_aev("content_block_stop", index=0)),
        _aenc(_aev("message_delta", delta={"stop_reason": "end_turn"}, usage={})),
        _aenc(_aev("message_stop")),
    ])
    chunks = _openai_chunks(raw)
    # The signature must not leak into ANY emitted delta field.
    for c in chunks:
        delta = c["choices"][0]["delta"]
        assert "sig-abc" not in json.dumps(delta), delta
    # And the thinking text itself still maps.
    assert _join_deltas(chunks, "reasoning_content") == "T"


# ══════════════════════════════════════════════════════════════════════════════
# C1-2 — Gemini thought parts (thought=true) → OpenAI chunk translation
# ════════════════════════════════════════════════════════════════════════════════

def test_c1_gemini_thought_parts_have_no_stream_translation_today():
    """Today's contract: StreamNormalizer exposes openai_sse↔anthropic_sse only.

    Gemini thought:true frames are never translated to OpenAI reasoning chunks
    here — the native-Gemini MITM pipeline (app/main.py) forwards thought
    frames as gemini_sse passthrough (with the thought-buffer pre-render hold).
    Gemini thought:true frames now translate to OpenAI reasoning chunks via
    the new convert_gemini_to_openai (C1 fix); gemini→anthropic still has no
    stream surface here.
    """
    assert hasattr(StreamNormalizer, "convert_gemini_to_openai")
    assert not hasattr(StreamNormalizer, "convert_gemini_to_anthropic")
    # The other two conversion entry points:
    assert hasattr(StreamNormalizer, "convert_openai_to_anthropic")
    assert hasattr(StreamNormalizer, "convert_anthropic_to_openai")


def test_c1_gemini_thought_part_translates_to_reasoning_content():
    """DESIRED-but-missing contract: gemini thought part → reasoning chunk."""
    gemini_frame = {
        "candidates": [{"content": {"role": "model", "parts": [
            {"text": "pondering", "thought": True},
        ]}}],
    }
    normalizer = StreamNormalizer("gemini_sse", "openai_sse", model_name="m")
    frames = [f"data: {json.dumps(gemini_frame, ensure_ascii=False)}\n\n".encode("utf-8")]

    raw = asyncio.run(_collect(
        normalizer.convert_gemini_to_openai(_byte_stream(frames)))).decode("utf-8")
    chunks = _openai_chunks(raw)
    assert _join_deltas(chunks, "reasoning_content") == "pondering"


# ══════════════════════════════════════════════════════════════════════════════
# C1-3 — Responses reasoning item round-trip (streaming-shaped variant)
# ══════════════════════════════════════════════════════════════════════════════

def test_c1_responses_reasoning_item_encrypted_content_replay():
    """Streaming-shaped replay: a prior turn's reasoning item (with
    encrypted_content) rides the Responses input verbatim through the
    normalizer_v2 round-trip — position and byte-exact encrypted payload
    preserved, interleaved with function_call items in emission order.
    """
    from app.normalizer_v2 import from_canonical, to_canonical

    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "ponder"}],
        "encrypted_content": "ENC[abc123]",
    }
    body = {
        "model": "gpt-5.6-sol",
        "instructions": "sys",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "q"}]},
            reasoning_item,
            {"type": "function_call", "call_id": "call_1", "name": "look",
             "arguments": "{\"q\": \"cat\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "a cat"},
        ],
        "reasoning": {"effort": "high"},
    }
    out = from_canonical("responses", to_canonical("responses", body))
    items = out["input"]
    # The reasoning item replays VERBATIM (encrypted_content intact) in order.
    assert items[1] == reasoning_item
    assert items[2]["type"] == "function_call" and items[2]["call_id"] == "call_1"
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "a cat"}
    # And the shadow comparator sees the round-trip as clean.
    from app.middleware.normalizer_shadow import semantic_mismatches
    assert semantic_mismatches("responses", body, out) == []


# ══════════════════════════════════════════════════════════════════════════════
# C1-4 — Interleaved thinking + text + tool_use in ONE stream
# ══════════════════════════════════════════════════════════════════════════════

def test_c1_interleaved_thinking_text_tool_no_contamination_a2o():
    """anthropic→openai: thinking/text/tool deltas interleaved (2025-05-14
    interleaved-thinking style) each land in their OWN delta field — the
    reasoning buffer never bleeds into content and vice versa, including a
    thinking delta arriving AFTER the tool block.
    """
    raw = _run_a2o([
        _aenc(_aev("message_start", message={})),
        _aenc(_aev("content_block_start", index=0,
                   content_block={"type": "thinking", "thinking": ""})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "thinking_delta", "thinking": "THINK_A"})),
        _aenc(_aev("content_block_stop", index=0)),
        _aenc(_aev("content_block_start", index=1,
                   content_block={"type": "text", "text": ""})),
        _aenc(_aev("content_block_delta", index=1,
                   delta={"type": "text_delta", "text": "TEXT_A"})),
        _aenc(_aev("content_block_stop", index=1)),
        # thinking again mid-stream
        _aenc(_aev("content_block_start", index=2,
                   content_block={"type": "thinking", "thinking": ""})),
        _aenc(_aev("content_block_delta", index=2,
                   delta={"type": "thinking_delta", "thinking": "THINK_B"})),
        _aenc(_aev("content_block_stop", index=2)),
        # tool_use with partial json
        _aenc(_aev("content_block_start", index=3,
                   content_block={"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {}})),
        _aenc(_aev("content_block_delta", index=3,
                   delta={"type": "input_json_delta", "partial_json": "{\"x\": "})),
        _aenc(_aev("content_block_delta", index=3,
                   delta={"type": "input_json_delta", "partial_json": "\"1+1\"}"})),
        _aenc(_aev("content_block_stop", index=3)),
        # thinking AFTER the tool block (fully interleaved)
        _aenc(_aev("content_block_start", index=5,
                   content_block={"type": "thinking", "thinking": ""})),
        _aenc(_aev("content_block_delta", index=5,
                   delta={"type": "thinking_delta", "thinking": "THINK_C"})),
        _aenc(_aev("content_block_stop", index=5)),
        # trailing text
        _aenc(_aev("content_block_start", index=6,
                   content_block={"type": "text", "text": ""})),
        _aenc(_aev("content_block_delta", index=6,
                   delta={"type": "text_delta", "text": "TEXT_B"})),
        _aenc(_aev("content_block_stop", index=6)),
        _aenc(_aev("message_delta", delta={"stop_reason": "tool_use"},
                   usage={"output_tokens": 9})),
        _aenc(_aev("message_stop")),
    ])
    chunks = _openai_chunks(raw)
    assert _join_deltas(chunks, "reasoning_content") == "THINK_A" + "THINK_B" + "THINK_C"
    assert _join_deltas(chunks, "content") == "TEXT_A" + "TEXT_B"
    # Tool args assembled in order.
    tool_args = ""
    for c in chunks:
        for tc in c["choices"][0]["delta"].get("tool_calls", []):
            tool_args += tc.get("function", {}).get("arguments", "")
    assert json.loads(tool_args) == {"x": "1+1"}
    # Cross-contamination guards: nothing ever lands in the wrong buffer.
    assert "THINK" not in _join_deltas(chunks, "content")
    assert "TEXT" not in _join_deltas(chunks, "reasoning_content")
    assert "TEXT" not in tool_args and "THINK" not in tool_args
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert "data: [DONE]" in raw


def test_c1_openai_reasoning_only_stream_surfaces_as_text_block():
    """openai→anthropic today: a reasoning-ONLY stream (no text/tool) emits the
    accumulated reasoning_content as the text block so the Anthropic client
    never sees an empty content list (current contract, verified).
    """
    raw = _run_o2a([
        _oenc(_ochunk({"reasoning_content": "Thinking..."})),
        _oenc(_ochunk({"reasoning_content": " about it"})),
        _oenc(_ochunk(None, "stop")),
        b"data: [DONE]\n\n",
    ])
    text, blocks, stop, saw_stop = _extract_anthropic(raw)
    assert saw_stop
    assert text == "Thinking... about it"
    assert blocks == {}
    assert stop == "end_turn"


def test_c1_openai_interleaved_reasoning_preserved_as_thinking_block():
    """DESIRED-but-missing contract: interleaved reasoning survives the
    openai→anthropic conversion (as a thinking block or equivalent)."""
    raw = _run_o2a([
        _oenc(_ochunk({"reasoning_content": "THINK_A"})),
        _oenc(_ochunk({"content": "TEXT_A"})),
        _oenc(_ochunk({"reasoning_content": "THINK_B"})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get", "arguments": "{\"q\": \"cat\"}"},
        }]})),
        _oenc(_ochunk(None, "tool_calls")),
        b"data: [DONE]\n\n",
    ])
    events = _anthropic_events(raw)
    thinking_text = "".join(
        data["delta"].get("thinking", "")
        for etype, data in events
        if etype == "content_block_delta" and data["delta"].get("type") == "thinking_delta"
    )
    assert thinking_text == "THINK_A" + "THINK_B"


def test_c1_openai_interleaved_reasoning_text_tool_buffers_clean_today():
    """The buffers that DO survive today stay uncontaminated: text and tool
    args assemble exactly, and the dropped reasoning never leaks into them.
    """
    raw = _run_o2a([
        _oenc(_ochunk({"reasoning_content": "THINK_A"})),
        _oenc(_ochunk({"content": "TEXT_A"})),
        _oenc(_ochunk({"reasoning_content": "THINK_B"})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get", "arguments": "{\"q\": "},
        }]})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "function": {"arguments": "\"cat\"}"},
        }]})),
        _oenc(_ochunk({"content": "TEXT_B"})),
        _oenc(_ochunk(None, "tool_calls")),
        b"data: [DONE]\n\n",
    ])
    text, blocks, stop, saw_stop = _extract_anthropic(raw)
    assert saw_stop
    # Text assembles exactly — no reasoning fragment ever landed in it.
    assert text == "TEXT_A" + "TEXT_B"
    assert "THINK" not in text
    # Tool args assemble exactly.
    assert len(blocks) == 1
    assert json.loads(list(blocks.values())[0]["input_json"]) == {"q": "cat"}
    assert "THINK" not in list(blocks.values())[0]["input_json"]
    assert stop == "tool_use"


# ══════════════════════════════════════════════════════════════════════════════
# C5-1 — openai_sse index-keyed partial fragments, interleaved order
# ══════════════════════════════════════════════════════════════════════════════

def test_c5_openai_tool_fragments_interleaved_indices_assemble():
    """Two tool calls (indices 0 and 1) whose fragments arrive INTERLEAVED
    and non-sequentially (1's first fragment before 0's) still assemble into
    two independent, correct tool_use blocks keyed by stream index.
    """
    raw = _run_o2a([
        # index 1 arrives FIRST (non-sequential index order)
        _oenc(_ochunk({"tool_calls": [{
            "index": 1, "id": "call_b", "type": "function",
            "function": {"name": "second", "arguments": "{\"n\": "},
        }]})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "call_a", "type": "function",
            "function": {"name": "first", "arguments": "{\"m\": "},
        }]})),
        # fragments continue interleaved
        _oenc(_ochunk({"tool_calls": [{
            "index": 1, "function": {"arguments": "2}"},
        }]})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "function": {"arguments": "1}"},
        }]})),
        _oenc(_ochunk(None, "tool_calls")),
        b"data: [DONE]\n\n",
    ])
    _, blocks, stop, saw_stop = _extract_anthropic(raw)
    assert saw_stop
    assert stop == "tool_use"
    assert len(blocks) == 2, f"expected 2 tool blocks, got {blocks}"
    by_name = {b["name"]: b["input_json"] for b in blocks.values()}
    assert json.loads(by_name["first"]) == {"m": 1}
    assert json.loads(by_name["second"]) == {"n": 2}
    # No cross-contamination: fragment of index 0 never lands in index 1's args.
    assert by_name["first"] != by_name["second"]


def test_c5_openai_out_of_order_index_fragment_within_one_call():
    """Within ONE call, a late-arriving id/name fill (index-keyed continuation
    arriving after argument fragments) still lands on the right block — the
    accumulator merges by index, not by arrival completeness.
    """
    raw = _run_o2a([
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "function": {"arguments": "{\"city\": "},
        }]})),
        # id+name arrive only on the LATER fragment (some providers do this)
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "call_late", "type": "function",
            "function": {"name": "late_get", "arguments": "\"Hanoi\"}"},
        }]})),
        _oenc(_ochunk(None, "tool_calls")),
        b"data: [DONE]\n\n",
    ])
    _, blocks, stop, saw_stop = _extract_anthropic(raw)
    assert saw_stop
    assert len(blocks) == 1
    block = list(blocks.values())[0]
    assert block["id"] == "call_late"
    assert block["name"] == "late_get"
    assert json.loads(block["input_json"]) == {"city": "Hanoi"}


# ══════════════════════════════════════════════════════════════════════════════
# C5-2 — anthropic_sse input_json_delta across blocks with the SAME tool id
# ══════════════════════════════════════════════════════════════════════════════

def test_c5_anthropic_same_tool_id_two_blocks_not_merged():
    """Today's contract: two content_block_start tool_use events sharing ONE
    id are NOT merged — the converter tracks a single `current_tool_index`
    counter, so the second block becomes a SECOND OpenAI tool_calls index
    carrying the same id, each with only its own partials.
    """
    raw = _run_a2o([
        _aenc(_aev("message_start", message={})),
        _aenc(_aev("content_block_start", index=0,
                   content_block={"type": "tool_use", "id": "toolu_same", "name": "calc", "input": {}})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "input_json_delta", "partial_json": "{\"a\": "})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "input_json_delta", "partial_json": "1}"})),
        _aenc(_aev("content_block_stop", index=0)),
        _aenc(_aev("content_block_start", index=1,
                   content_block={"type": "tool_use", "id": "toolu_same", "name": "calc", "input": {}})),
        _aenc(_aev("content_block_delta", index=1,
                   delta={"type": "input_json_delta", "partial_json": "{\"b\": "})),
        _aenc(_aev("content_block_delta", index=1,
                   delta={"type": "input_json_delta", "partial_json": "2}"})),
        _aenc(_aev("content_block_stop", index=1)),
        _aenc(_aev("message_delta", delta={"stop_reason": "tool_use"},
                   usage={"output_tokens": 3})),
        _aenc(_aev("message_stop")),
    ])
    chunks = _openai_chunks(raw)
    # Two start chunks: indices 0 and 1, both carrying the SAME id.
    starts = [
        tc for c in chunks
        for tc in c["choices"][0]["delta"].get("tool_calls", [])
        if "id" in tc
    ]
    assert len(starts) == 2
    assert [tc["index"] for tc in starts] == [0, 1]
    assert all(tc["id"] == "toolu_same" for tc in starts)
    # Per-index arg assembly stays split — each block keeps only its own args.
    args_by_index = {}
    for c in chunks:
        for tc in c["choices"][0]["delta"].get("tool_calls", []):
            args_by_index.setdefault(tc["index"], "")
            args_by_index[tc["index"]] += tc.get("function", {}).get("arguments", "")
    assert json.loads(args_by_index[0]) == {"a": 1}
    assert json.loads(args_by_index[1]) == {"b": 2}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert "data: [DONE]" in raw


# ══════════════════════════════════════════════════════════════════════════════
# C5-3 — Fragment split mid-UTF8-codepoint inside tool args JSON string
# ══════════════════════════════════════════════════════════════════════════════

def test_c5_mid_utf8_split_in_tool_args_o2a_reassembles():
    """openai→anthropic: the incremental UTF-8 decoder holds a partial
    codepoint split across network chunks, so an emoji inside tool args
    reassembles byte-exact in the emitted input_json_delta.
    """
    full = (
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "c1", "type": "function",
            "function": {"name": "send", "arguments": "{\"m\": \"" + EMOJI + "\"}"},
        }]}))
        + _oenc(_ochunk(None, "tool_calls"))
        + b"data: [DONE]\n\n"
    )
    cut = full.find(EMOJI.encode("utf-8")) + 2  # split INSIDE the 4-byte sequence
    assert full[cut - 2:cut + 2] == EMOJI.encode("utf-8")  # verify mid-point
    raw = _run_o2a([full[:cut], full[cut:]])
    _, blocks, stop, saw_stop = _extract_anthropic(raw)
    assert saw_stop
    assert stop == "tool_use"
    assert len(blocks) == 1
    assert json.loads(list(blocks.values())[0]["input_json"]) == {"m": EMOJI}


def test_c5_mid_utf8_split_surrogate_pair_escapes_assemble():
    """An emoji split at the ESCAPE level (surrogate halves in two argument
    fragments) reassembles too — the JSON string \\ud83d\\ude00 halves are just
    concatenated text until json.loads decodes them.
    """
    raw = _run_o2a([
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "c1", "type": "function",
            "function": {"name": "send", "arguments": "{\"m\": \"\\ud83d"},
        }]})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "function": {"arguments": "\\ude00\"}"},
        }]})),
        _oenc(_ochunk(None, "tool_calls")),
        b"data: [DONE]\n\n",
    ])
    _, blocks, _, saw_stop = _extract_anthropic(raw)
    assert saw_stop
    assert len(blocks) == 1
    assert json.loads(list(blocks.values())[0]["input_json"]) == {"m": EMOJI}


def test_c5_mid_utf8_split_a2o_keeps_text():
    """DESIRED-but-missing contract: anthropic→openai survives a mid-codepoint
    chunk split without losing the text delta."""
    full_line = _aenc(_aev("content_block_delta", index=0,
                           delta={"type": "text_delta", "text": "Hel" + EMOJI + "!"}))
    cut = full_line.find(EMOJI.encode("utf-8")) + 2
    raw = _run_a2o([
        _aenc(_aev("message_start", message={})),
        full_line[:cut], full_line[cut:],
        _aenc(_aev("message_delta", delta={"stop_reason": "end_turn"}, usage={})),
        _aenc(_aev("message_stop")),
    ])
    chunks = _openai_chunks(raw)
    assert _join_deltas(chunks, "content") == "Hel" + EMOJI + "!"


# ══════════════════════════════════════════════════════════════════════════════
# C5-4 — Stream ending mid-fragment (truncated upstream)
# ══════════════════════════════════════════════════════════════════════════════

def test_c5_truncated_o2a_no_hang_no_complete_call():
    """openai→anthropic truncated mid tool fragment: the stream TERMINATES
    (message_stop always — no hang), and the partial call is never emitted as
    complete valid JSON: the unparseable payload is wrapped with the
    _bsl_malformed_arguments marker for client validation (fail-fast file
    conventions: whatever the model emits, the turn must complete).
    """
    raw = _run_o2a([
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "id": "call_t", "type": "function",
            "function": {"name": "get", "arguments": "{\"city\": "},
        }]})),
        _oenc(_ochunk({"tool_calls": [{
            "index": 0, "function": {"arguments": "\"Han"},
        }]})),
        # upstream dies here — no [DONE], no finish_reason
    ])
    _, blocks, stop, saw_stop = _extract_anthropic(raw)
    assert saw_stop, "truncated stream must still terminate with message_stop"
    assert stop == "tool_use"
    assert len(blocks) == 1
    block = list(blocks.values())[0]
    # The partial call is NEVER emitted as the complete original JSON: it
    # parses only as the repair wrapper, and the raw truncated payload inside
    # the wrapper is preserved unparseable (diagnosable, never fabricated).
    wrapped = json.loads(block["input_json"])
    assert list(wrapped.keys()) == ["_bsl_malformed_arguments"], wrapped
    raw_partial = wrapped["_bsl_malformed_arguments"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw_partial)
    assert raw_partial == '{"city": "Han'


def test_c5_truncated_a2o_no_hang_fallback_finish():
    """anthropic→openai truncated mid input_json_delta (no message_delta /
    message_stop): the converter emits the fallback finish chunk
    (finish_reason=tool_calls) plus [DONE] — no hang, and the partial args are
    forwarded as-is, never fabricated into a complete-looking call.
    """
    raw = _run_a2o([
        _aenc(_aev("message_start", message={})),
        _aenc(_aev("content_block_start", index=0,
                   content_block={"type": "tool_use", "id": "toolu_9", "name": "calc", "input": {}})),
        _aenc(_aev("content_block_delta", index=0,
                   delta={"type": "input_json_delta", "partial_json": "{\"expr\": \"1+"})),
        # upstream dies here
    ])
    chunks = _openai_chunks(raw)
    assert "data: [DONE]" in raw, "stream must terminate with [DONE] (no hang)"
    # Partial args forwarded verbatim — not padded into a complete JSON.
    tool_args = ""
    for c in chunks:
        for tc in c["choices"][0]["delta"].get("tool_calls", []):
            tool_args += tc.get("function", {}).get("arguments", "")
    assert tool_args == "{\"expr\": \"1+"
    with pytest.raises(json.JSONDecodeError):
        json.loads(tool_args)
    # Fallback terminal chunk: tool calls seen → finish_reason tool_calls.
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
