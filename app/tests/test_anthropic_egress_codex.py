"""Regression coverage for Codex Responses SSE on Anthropic egress."""
import asyncio
import json

import app.main as main
from app.compat.stream_normalizer import StreamNormalizer


def _sse(events):
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
        for name, data in events
    )


async def _aiter(payload):
    yield payload


def test_codex_anthropic_egress_emits_text_tool_and_usage():
    payload = _sse([
        ("response.output_text.delta", {"delta": "hello"}),
        ("response.output_item.done", {"item": {"type": "function_call", "id": "call_1", "name": "lookup", "arguments": "{\"q\":1}"}}),
        ("response.completed", {"response": {"model": "codex", "output": [{"type": "function_call", "id": "call_1"}], "usage": {"input_tokens": 5, "output_tokens": 7}}}),
    ])

    async def collect():
        source = main._anthropic_egress_openai_source("codex", _aiter(payload))
        normalizer = StreamNormalizer("openai_sse", "anthropic_sse", model_name="codex")
        return b"".join([chunk async for chunk in normalizer.convert_openai_to_anthropic(source)])

    text = asyncio.run(collect()).decode("utf-8")
    assert "content_block_delta" in text
    assert '"text_delta"' in text and "hello" in text
    assert '"type": "tool_use"' in text
    assert '"output_tokens": 7' in text
    assert "message_stop" in text
