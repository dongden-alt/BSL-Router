"""
Regression tests for the 2026-07-31 Anthropic→OpenAI converter fixes:
 1. Repeated same-tool calls in one response must get UNIQUE tool_use ids
    (was: call_{name} collapsed all invocations onto one id → Anthropic 400
    on the next turn when client returned duplicate tool_use_ids).
 2. message_delta usage must be forwarded to the OpenAI client (was dropped).
"""
import asyncio
import json
import pytest

from app.compat.stream_normalizer import StreamNormalizer


def _anthropic_sse(events):
    out = b""
    for etype, data in events:
        out += f"event: {etype}\ndata: {json.dumps(data)}\n\n".encode()
    return out


async def _aiter(b: bytes):
    yield b


def _parse_openai_frames(raw: bytes):
    frames = []
    for line in raw.decode().split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line[6:] != "[DONE]":
            frames.append(json.loads(line[6:]))
    return frames


def test_repeated_same_tool_gets_unique_ids():
    events = [
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 42}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"command":"pwd"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 17}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    n = StreamNormalizer(source_dialect="anthropic_sse", target_dialect="openai_sse", model_name="m")

    async def run():
        out = b""
        async for chunk in n.convert_anthropic_to_openai(_aiter(_anthropic_sse(events))):
            out += chunk
        return out

    frames = _parse_openai_frames(asyncio.run(run()))

    # Collect tool_call ids and their accumulated args per openai index
    ids = {}
    args = {}
    for f in frames:
        for tc in (f["choices"][0].get("delta", {}).get("tool_calls") or []):
            if tc.get("id"):
                ids[tc["index"]] = tc["id"]
            frag = tc.get("function", {}).get("arguments", "")
            args[tc["index"]] = args.get(tc["index"], "") + frag

    assert len(ids) == 2, f"expected 2 tool calls, got {ids}"
    assert ids[0] != ids[1], f"duplicate tool ids: {ids}"
    assert args[0] == '{"command":"ls"}', args
    assert args[1] == '{"command":"pwd"}', args

    # finish frame carries usage (fix 2)
    finish_frames = [f for f in frames if f["choices"][0].get("finish_reason")]
    assert finish_frames, "no finish frame"
    usage = finish_frames[-1].get("usage")
    assert usage and usage["prompt_tokens"] == 42 and usage["completion_tokens"] == 17, usage

    # finish_reason mapped from tool_use
    assert finish_frames[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_grok_full_input_in_block_start_still_works():
    """xAI/Grok puts the full input in content_block_start (regression guard)."""
    events = [
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "toolu_x", "name": "search",
                                                   "input": {"q": "hello"}}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}),
    ]
    n = StreamNormalizer(source_dialect="anthropic_sse", target_dialect="openai_sse", model_name="m")

    async def run():
        out = b""
        async for chunk in n.convert_anthropic_to_openai(_aiter(_anthropic_sse(events))):
            out += chunk
        return out

    frames = _parse_openai_frames(asyncio.run(run()))
    args = ""
    for f in frames:
        for tc in (f["choices"][0].get("delta", {}).get("tool_calls") or []):
            args += tc.get("function", {}).get("arguments", "")
    assert json.loads(args) == {"q": "hello"}, args
