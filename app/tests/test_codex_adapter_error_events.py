"""Regression coverage for Codex Responses SSE error and tool events."""
import asyncio
import json

from app.codex_adapter import responses_sse_to_openai_sse


def _sse(events):
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
        for name, data in events
    )


async def _aiter(payload):
    yield payload


def _frames(payload):
    async def collect():
        return b"".join([chunk async for chunk in responses_sse_to_openai_sse(_aiter(payload))])
    raw = asyncio.run(collect()).decode()
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


def test_response_failed_is_visible_and_terminal():
    frames = _frames(_sse([
        ("response.failed", {"response": {"model": "codex", "error": {"message": "upstream boom"}}}),
    ]))
    assert frames
    assert any("upstream boom" in c.get("choices", [{}])[0].get("delta", {}).get("content", "") for c in frames)
    assert any(c["choices"][0]["finish_reason"] == "error" for c in frames)


def test_healthy_tool_call_and_usage_are_preserved():
    frames = _frames(_sse([
        ("response.output_item.done", {"item": {"type": "function_call", "id": "call_1", "name": "lookup", "arguments": "{\"q\":1}"}}),
        ("response.completed", {"response": {"model": "codex", "output": [{"type": "function_call", "id": "call_1"}], "usage": {"input_tokens": 3, "output_tokens": 4}}}),
    ]))
    assert any(c["choices"][0]["delta"].get("tool_calls") for c in frames)
    finish = frames[-1]
    assert finish["choices"][0]["finish_reason"] == "tool_calls"
    assert finish["usage"]["completion_tokens"] == 4


def test_response_incomplete_is_surfaced():
    frames = _frames(_sse([
        ("response.incomplete", {"response": {"incomplete_details": {"reason": "max_output_tokens"}}}),
    ]))
    assert any("max_output_tokens" in c.get("choices", [{}])[0].get("delta", {}).get("content", "") for c in frames)
    assert any(c["choices"][0]["finish_reason"] == "error" for c in frames)
