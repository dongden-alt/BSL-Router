"""
Bug A regression tests — streaming tool-call rescue (2026-08-04).

Symptom fixed: GLM/Opus/Sonnet sometimes emit tool calls as inline
text-delimiter markup (<tool_call>... or unicode variants) instead of
structured tool_calls. The buffered path repaired this via
normalize_glm_tool_calls, but the STREAMING path forwarded the markup as
text_delta, so the IDE rendered raw tool-call text.

These tests drive StreamNormalizer.convert_openai_to_anthropic with OpenAI
SSE text deltas containing markup and assert:
  1. Markup is rescued into structured tool_use blocks (no raw leak).
  2. Buffering survives arbitrary delta splits (delimiter may span chunks).
  3. The rescue is GATED on tools_in_request (tool-less chats passthrough).
  4. Fail-open: unparseable/unclosed blocks emit as text, never swallowed,
     and the stream always reaches message_stop.
"""
import asyncio
import json

from app.compat.stream_normalizer import StreamNormalizer


def _openai_sse_chunk(content=None, tool_calls=None, finish=None, usage=None):
    """Build one OpenAI streaming chunk as SSE bytes."""
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [choice],
    }
    if usage:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _aiter(chunks):
    for c in chunks:
        yield c


def _run(chunks, tools_in_request=True):
    n = StreamNormalizer(
        "openai_sse", "anthropic_sse", model_name="m",
        tools_in_request=tools_in_request,
    )

    async def go():
        out = b""
        async for chunk in n.convert_openai_to_anthropic(_aiter(chunks)):
            out += chunk
        return out

    return asyncio.run(go())


def _parse_anthropic_events(raw: bytes):
    """Parse Anthropic SSE bytes into (event_type, data) pairs."""
    events = []
    text = raw.decode("utf-8")
    etype = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            etype = line[7:].strip()
        elif line.startswith("data: "):
            events.append((etype, json.loads(line[6:])))
            etype = None
    return events


def _extract(events):
    """Return (text, tool_blocks, stop_reason, saw_message_stop)."""
    text = ""
    blocks = {}  # index -> {name, id, input_json}
    stop_reason = None
    saw_stop = False
    for etype, data in events:
        if etype == "content_block_start":
            cb = data.get("content_block", {})
            if cb.get("type") == "tool_use":
                blocks[data["index"]] = {
                    "name": cb.get("name"),
                    "id": cb.get("id"),
                    "input_json": "",
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


# ── 1. Basic ASCII rescue ────────────────────────────────────────────────────

def test_ascii_tool_call_rescued_in_single_delta():
    """A complete <tool_call> block in one text delta is rescued."""
    chunks = [
        _openai_sse_chunk(content='I will read the file. <tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>'),
        _openai_sse_chunk(finish="stop"),
        b"data: [DONE]\n\n",
    ]
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks)))
    assert saw_stop, "stream must terminate with message_stop"
    assert "<tool_call>" not in text, "raw markup must not leak into text"
    assert "I will read the file." in text, "pre-block text must be preserved"
    assert len(blocks) == 1, f"expected 1 tool block, got {blocks}"
    assert list(blocks.values())[0]["name"] == "read_file"
    assert json.loads(list(blocks.values())[0]["input_json"]) == {"path": "a.txt"}
    assert stop == "tool_use", f"stop_reason must be tool_use, got {stop}"


# ── 2. Split across deltas ───────────────────────────────────────────────────

def test_ascii_tool_call_split_across_deltas():
    """The opener, body, and closer arrive in separate chunks."""
    parts = [
        'I will run it. <tool',
        '_call>{"name": "run',
        '_command", "arguments": {"cmd": "ls"}}</tool',
        '_call>',
    ]
    chunks = [_openai_sse_chunk(content=p) for p in parts]
    chunks.append(_openai_sse_chunk(finish="stop"))
    chunks.append(b"data: [DONE]\n\n")
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks)))
    assert saw_stop
    assert "<tool" not in text and "_call>" not in text, "markup must not leak"
    assert "I will run it." in text
    assert len(blocks) == 1
    assert list(blocks.values())[0]["name"] == "run_command"
    assert json.loads(list(blocks.values())[0]["input_json"]) == {"cmd": "ls"}
    assert stop == "tool_use"


# ── 3. Gate: no tools in request = passthrough ───────────────────────────────

def test_no_tools_in_request_passes_text_through():
    """When tools_in_request=False, markup is forwarded as text (today's behavior)."""
    chunks = [
        _openai_sse_chunk(content='Sure. <tool_call>{"name": "foo", "arguments": {}}</tool_call>'),
        _openai_sse_chunk(finish="stop"),
        b"data: [DONE]\n\n",
    ]
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks, tools_in_request=False)))
    assert saw_stop
    assert "<tool_call>" in text, "without tools, markup must pass through as text"
    assert len(blocks) == 0
    assert stop == "end_turn"


# ── 4. Unicode delimiter rescue ──────────────────────────────────────────────

def test_unicode_delimiter_rescued():
    """GLM/DeepSeek unicode-delimiter blocks are rescued."""
    uni_open = "<\uff5ctool\u2581calls\uff5cbegin\uff5c>"
    uni_close = "<\uff5ctool\u2581calls\uff5cend\uff5c>"
    body = '{"name": "search", "arguments": {"q": "hello"}}'
    chunks = [
        _openai_sse_chunk(content=f"Result: {uni_open}{body}{uni_close}"),
        _openai_sse_chunk(finish="stop"),
        b"data: [DONE]\n\n",
    ]
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks)))
    assert saw_stop
    assert uni_open not in text, "unicode markup must not leak"
    assert "Result:" in text
    assert len(blocks) == 1
    assert list(blocks.values())[0]["name"] == "search"
    assert stop == "tool_use"


# ── 5. Fail-open: unclosed block at EOF ──────────────────────────────────────

def test_unclosed_block_at_eof_emitted_as_text():
    """An opener without a closer is forwarded as text (fail-open)."""
    chunks = [
        _openai_sse_chunk(content='Text <tool_call>{"name": "incomplete"'),
        _openai_sse_chunk(finish="stop"),
        b"data: [DONE]\n\n",
    ]
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks)))
    assert saw_stop, "stream must still terminate cleanly"
    assert len(blocks) == 0, "unclosed block must not produce a tool call"
    # The incomplete markup is emitted as text (never swallowed)
    assert "<tool_call>" in text


# ── 6. Structured + text-form in one stream ──────────────────────────────────

def test_structured_and_text_form_mixed():
    """A stream with both structured tool_calls and text-form markup handles both."""
    chunks = [
        _openai_sse_chunk(content='Here. <tool_call>{"name": "text_tool", "arguments": {}}</tool_call>'),
        _openai_sse_chunk(tool_calls=[{"index": 0, "id": "call_struct", "type": "function",
                                       "function": {"name": "struct_tool", "arguments": "{}"}}]),
        _openai_sse_chunk(finish="stop"),
        b"data: [DONE]\n\n",
    ]
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks)))
    assert saw_stop
    assert "<tool_call>" not in text
    assert "Here." in text
    assert len(blocks) == 2, f"expected 2 tool blocks (1 rescued + 1 structured), got {len(blocks)}"
    names = sorted(b["name"] for b in blocks.values())
    assert names == ["struct_tool", "text_tool"], names
    assert stop == "tool_use"


# ── 7. Plain text with angle brackets is not falsely buffered ────────────────

def test_plain_text_with_angles_not_buffered():
    """Text containing '<' but no tool-call markup passes through cleanly."""
    chunks = [
        _openai_sse_chunk(content="The value is < 5 and > 3. Clear?"),
        _openai_sse_chunk(finish="stop"),
        b"data: [DONE]\n\n",
    ]
    text, blocks, stop, saw_stop = _extract(_parse_anthropic_events(_run(chunks)))
    assert saw_stop
    assert text == "The value is < 5 and > 3. Clear?"
    assert len(blocks) == 0
    assert stop == "end_turn"
