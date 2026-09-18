"""Test suite for FelStreamObserver — realtime stream refusal classification."""
import json
from typing import List
from unittest.mock import patch

import pytest

from app.middleware.fel_wiring import FelStreamObserver


# Helper: capture fel_event calls
class FelEventCapture:
    def __init__(self):
        self.calls: List[dict] = []

    def capture(self, kind: str, model: str = "", provider: str = "",
                details: dict = None) -> None:
        self.calls.append({
            "kind": kind,
            "model": model,
            "provider": provider,
            "details": details or {},
        })


def anthropic_text_delta(text: str) -> bytes:
    """Build an Anthropic text_delta SSE chunk."""
    obj = {"delta": {"type": "text_delta", "text": text}}
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n"


def openai_content_delta(content: str) -> bytes:
    """Build an OpenAI choices[].delta.content SSE chunk."""
    obj = {"choices": [{"delta": {"content": content}}]}
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n"


def anthropic_tool_use_start() -> bytes:
    """Build an Anthropic content_block_start with tool_use."""
    obj = {"content_block": {"type": "tool_use", "id": "toolu_123"}}
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n"


def openai_tool_calls_delta() -> bytes:
    """Build an OpenAI choices[].delta.tool_calls chunk."""
    obj = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "test"}}]}}]}
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n"


def test_anthropic_refusal_two_markers():
    """Anthropic text_deltas with >=2 DISTINCT refusal markers -> refusal."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="claude-opus-4", provider="anthropic")
        # Two distinct markers: "I cannot" + "I'm not able to"
        obs.observe(anthropic_text_delta("I cannot help with that request. "))
        obs.observe(anthropic_text_delta("I'm not able to assist with this."))
        verdict = obs.finalize()

    assert verdict == "refusal"
    assert len(cap.calls) == 1
    assert cap.calls[0]["kind"] == "refusal"
    assert cap.calls[0]["details"]["classification"] == "refusal"
    assert cap.calls[0]["details"]["stream"] is True
    assert cap.calls[0]["details"]["realtime"] is True
    assert cap.calls[0]["details"]["tool_calls"] is False


def test_openai_refusal_two_markers():
    """OpenAI choices[].delta.content with >=2 DISTINCT markers -> refusal."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="gpt-5.6", provider="openai")
        obs.observe(openai_content_delta("I must decline this request. "))
        obs.observe(openai_content_delta("It is against my policy."))
        verdict = obs.finalize()

    assert verdict == "refusal"
    assert len(cap.calls) == 1
    assert cap.calls[0]["details"]["classification"] == "refusal"


def test_single_marker_yields_unknown_but_emits():
    """Single marker refusal -> unknown, but event IS still emitted."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        # Only "I cannot" (one marker)
        obs.observe(anthropic_text_delta("I cannot and will not help with that. "))
        obs.observe(anthropic_text_delta("I'm unable to assist with this request."))
        verdict = obs.finalize()

    # "I cannot" + "I'm unable to" -> 2 markers -> refusal
    # Let me fix the test: single marker only
    cap2 = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap2.capture):
        obs2 = FelStreamObserver(model="test", provider="test")
        obs2.observe(anthropic_text_delta("I cannot help with that request."))
        verdict2 = obs2.finalize()

    assert verdict2 == "unknown"
    assert len(cap2.calls) == 1
    assert cap2.calls[0]["details"]["classification"] == "unknown"


def test_clean_technical_text():
    """Normal technical text -> clean."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        obs.observe(anthropic_text_delta("Here's a Python function that implements "))
        obs.observe(anthropic_text_delta("binary search on a sorted array."))
        verdict = obs.finalize()

    assert verdict == "clean"
    assert len(cap.calls) == 1
    assert cap.calls[0]["details"]["classification"] == "clean"


def test_tool_calls_not_refusal_anthropic():
    """Stream with Anthropic tool_use -> not a refusal."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="claude", provider="anthropic")
        obs.observe(anthropic_text_delta("I'll help you with that. "))
        obs.observe(anthropic_tool_use_start())
        obs.observe(anthropic_text_delta("Let me search for the information."))
        verdict = obs.finalize()

    assert verdict == "clean"
    assert cap.calls[0]["details"]["tool_calls"] is True


def test_tool_calls_not_refusal_openai():
    """Stream with OpenAI tool_calls -> not a refusal."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="gpt-5", provider="openai")
        obs.observe(openai_content_delta("I'll search for that information."))
        obs.observe(openai_tool_calls_delta())
        verdict = obs.finalize()

    assert verdict == "clean"
    assert cap.calls[0]["details"]["tool_calls"] is True


def test_thinking_deltas_only_no_event():
    """Thinking/reasoning deltas only (no visible text) -> no event, no crash."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        # Simulate thinking deltas (not text_delta)
        thinking = {"delta": {"type": "thinking", "text": "Let me analyze..."}}
        obs.observe(b"data: " + json.dumps(thinking).encode("utf-8") + b"\n")
        verdict = obs.finalize()

    assert verdict == ""
    assert len(cap.calls) == 0


def test_large_text_accumulation_caps():
    """100 KB of text -> accumulation caps at 16 KB, no error."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test", max_chars=16384)
        # Send 100 KB of data
        chunk = "A" * 1000
        for _ in range(100):
            obs.observe(anthropic_text_delta(chunk))
        verdict = obs.finalize()

    # Should cap at max_chars (16384)
    assert cap.calls[0]["details"]["chars"] <= 16384


def test_partial_sse_frame_split():
    """One SSE frame split across 3 byte chunks -> text still extracted."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        full_line = anthropic_text_delta("Hello world")
        # Split into 3 parts
        part1 = full_line[:10]
        part2 = full_line[10:20]
        part3 = full_line[20:]
        obs.observe(part1)
        obs.observe(part2)
        obs.observe(part3)
        verdict = obs.finalize()

    # Should successfully extract "Hello world"
    assert cap.calls[0]["details"]["chars"] == 11


def test_garbage_chunks_no_crash():
    """Garbage / non-JSON / truncated chunks -> never raises."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        obs.observe(b"data: {invalid json\n")
        obs.observe(b"data: \xFF\xFE\n")
        obs.observe(b"random garbage\n")
        obs.observe(anthropic_text_delta("Valid text"))
        verdict = obs.finalize()

    # Should still extract the valid chunk
    assert verdict == "clean"
    assert cap.calls[0]["details"]["chars"] == 10


def test_finalize_idempotent():
    """finalize() called twice -> exactly one event."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        obs.observe(anthropic_text_delta("Test message"))
        verdict1 = obs.finalize()
        verdict2 = obs.finalize()

    assert verdict1 == "clean"
    assert verdict2 == ""  # Second call returns empty
    assert len(cap.calls) == 1  # Only one event emitted


def test_empty_stream_no_event():
    """Empty stream -> no event, returns empty string."""
    cap = FelEventCapture()
    with patch("app.middleware.fel_wiring.fel_event", cap.capture):
        obs = FelStreamObserver(model="test", provider="test")
        verdict = obs.finalize()

    assert verdict == ""
    assert len(cap.calls) == 0
