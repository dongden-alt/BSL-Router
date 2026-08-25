"""Tests for the AWS event-stream binary decoder (kiro_eventstream).

The kiro gateway's /generateAssistantResponse returns binary
vnd.amazon.eventstream frames. These tests build REAL frames (same layout
the gateway produces — verified against a live 200 capture 2026-08-25) and
verify decode -> OpenAI completion/SSE.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import kiro_eventstream  # noqa: E402


def _make_header(name: bytes, value: bytes) -> bytes:
    # string value type 7: name-len(1) name vtype(1) vlen(2 BE) value
    return bytes([len(name)]) + name + b"\x07" + len(value).to_bytes(2, "big") + value


def _make_frame(event_type: str, payload: bytes) -> bytes:
    headers = (
        _make_header(b":event-type", event_type.encode())
        + _make_header(b":content-type", b"application/json")
    )
    body = headers + payload
    # total-len(4) headers-len(4) prelude-crc(4, zeros ok for our decoder) headers payload crc(4, zeros)
    frame_len = 12 + len(body) + 4
    return (
        frame_len.to_bytes(4, "big")
        + len(headers).to_bytes(4, "big")
        + b"\x00\x00\x00\x00"
        + body
        + b"\x00\x00\x00\x00"
    )


def test_is_eventstream_detects_binary():
    frame = _make_frame("assistantResponseEvent", b"{}")
    assert kiro_eventstream.is_eventstream(frame) is True
    assert kiro_eventstream.is_eventstream(b"event: foo\n") is False


def test_parse_frames_roundtrip():
    e1 = _make_frame("assistantResponseEvent", json.dumps({"content": "Hello"}).encode())
    e2 = _make_frame(
        "codeWhispererMetadataEvent",
        json.dumps({"usage": {"inputTokens": 5, "outputTokens": 2}}).encode(),
    )
    events, rem = kiro_eventstream.parse_eventstream_frames(e1 + e2)
    assert rem == b""
    assert len(events) == 2
    assert events[0][0] == "assistantResponseEvent"
    assert json.loads(events[0][1])["content"] == "Hello"
    assert events[1][0] == "codeWhispererMetadataEvent"


def test_partial_frame_buffered():
    e1 = _make_frame("assistantResponseEvent", json.dumps({"content": "Hi"}).encode())
    half = e1[: len(e1) // 2]
    events, rem = kiro_eventstream.parse_eventstream_frames(half)
    assert events == []
    assert rem == half
    events2, rem2 = kiro_eventstream.parse_eventstream_frames(rem + e1[len(e1) // 2:])
    assert len(events2) == 1


def test_completion_assembly():
    e1 = _make_frame("assistantResponseEvent", json.dumps({"content": "Hello "}).encode())
    e2 = _make_frame("assistantResponseEvent", json.dumps({"content": "world", "modelId": "claude-sonnet-4.5"}).encode())
    e3 = _make_frame(
        "codeWhispererMetadataEvent",
        json.dumps({"usage": {"inputTokens": 5, "outputTokens": 2}}).encode(),
    )
    out = kiro_eventstream.eventstream_to_openai_completion(e1 + e2 + e3)
    assert out is not None
    assert out["choices"][0]["message"]["content"] == "Hello world"
    assert out["model"] == "claude-sonnet-4.5"
    assert out["usage"]["prompt_tokens"] == 5
    assert out["usage"]["completion_tokens"] == 2


def test_completion_none_on_garbage():
    assert kiro_eventstream.eventstream_to_openai_completion(b"not binary") is None


def test_completion_none_on_json_body():
    # Legacy JSON responses (older captures/tests) must not be mangled
    assert kiro_eventstream.eventstream_to_openai_completion(json.dumps({"response": {"content": "x"}}).encode()) is None


def test_sse_generator_decodes_binary():
    e1 = _make_frame("assistantResponseEvent", json.dumps({"content": "OK"}).encode())
    e2 = _make_frame("codeWhispererMetadataEvent", json.dumps({"usage": {"inputTokens": 1, "outputTokens": 1}}).encode())

    async def chunks():
        # deliberately split mid-frame to test buffering
        yield e1[:7]
        yield e1[7:]
        yield e2

    async def run():
        return [c async for c in kiro_eventstream.eventstream_to_openai_sse_lines(chunks())]

    out = asyncio.run(run())
    assert len(out) >= 1
    first = json.loads(out[0][6:].decode())
    assert first["choices"][0]["delta"]["content"] == "OK"


def test_fallback_sniffs_text_sse():
    async def chunks():
        yield b"event: codeWhispererResponseEvent\ndata: {\"content\": \"legacy\"}\n\n"

    async def run():
        return [c async for c in kiro_eventstream.eventstream_to_openai_sse_lines_with_fallback(chunks())]

    out = asyncio.run(run())
    assert len(out) == 1
    parsed = json.loads(out[0][6:].decode())
    assert parsed["choices"][0]["delta"]["content"] == "legacy"
