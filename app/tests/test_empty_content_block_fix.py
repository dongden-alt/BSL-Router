"""Never emit an empty-text-only Anthropic response.

Two guarantees:
  1. Non-stream: when content=="" but reasoning_content is present, the
     reasoning IS the deliverable and must be emitted as the text block.
     When both are empty, a text:"" fallback is preserved (the legacy
     terminal contract).
  2. Stream: a reasoning-only OpenAI SSE (no content deltas) must produce
     a well-formed Anthropic SSE with content_block_start +
     content_block_delta(text=reasoning) + content_block_stop BEFORE
     message_delta/message_stop. A normal text-only stream is untouched.
"""
from __future__ import annotations

import asyncio
import json

from app.normalizer import UniversalNormalizer
from app.compat.stream_normalizer import StreamNormalizer


# ─── helpers ──────────────────────────────────────────────────────────────────

def _openai_resp(content: str | None, reasoning: str | None, finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "model": "gpt-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    **(
                        {"reasoning_content": reasoning}
                        if reasoning is not None
                        else {}
                    ),
                },
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _openai_sse(chunks: list[dict]) -> list[bytes]:
    return [f"data: {json.dumps(c)}\n\n".encode() for c in chunks] + [b"data: [DONE]\n\n"]


async def _collect(stream):
    return b"".join([chunk async for chunk in stream])


async def _byte_stream(frames: list[bytes]):
    for f in frames:
        yield f


def _stream_convert(frames: list[bytes]) -> str:
    norm = StreamNormalizer("openai_sse", "anthropic_sse", model_name="gpt-test")
    out = asyncio.run(_collect(norm.convert_openai_to_anthropic(_byte_stream(frames))))
    return out.decode("utf-8")


# ─── non-stream ───────────────────────────────────────────────────────────────

def test_reasoning_fallback_when_content_empty():
    """content='' + reasoning_content='R' => content == [{'type':'text','text':'R'}]."""
    resp = _openai_resp(content="", reasoning="R")
    out = UniversalNormalizer.openai_response_to_anthropic(resp, model="gpt-test")
    assert out["content"] == [{"type": "text", "text": "R"}]


def test_both_empty_preserves_text_fallback():
    """content='' and no reasoning_content => text:'' fallback preserved."""
    resp = _openai_resp(content="", reasoning=None)
    out = UniversalNormalizer.openai_response_to_anthropic(resp, model="gpt-test")
    assert out["content"] == [{"type": "text", "text": ""}]


# ─── stream ───────────────────────────────────────────────────────────────────

def test_reasoning_only_stream_emits_text_block_before_stop():
    """OpenAI SSE with ONLY reasoning_content deltas must yield a complete
    Anthropic text block (start + delta + stop) before message_delta/message_stop.
    """
    frames = _openai_sse(
        [
            {
                "id": "chatcmpl-r",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {"role": "assistant", "reasoning_content": "R"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-r",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
    )
    text = _stream_convert(frames)

    # The reasoning must surface as a text block.
    assert "event: content_block_start" in text
    assert "event: content_block_delta" in text
    assert '"text_delta"' in text
    assert "R" in text
    assert "event: content_block_stop" in text
    # And the block must close before the stream terminates.
    assert text.index("event: content_block_start") < text.index("event: content_block_stop")
    assert text.index("event: content_block_stop") < text.index("event: message_stop")


def test_text_only_stream_unchanged():
    """A normal text-only stream must produce exactly one text block with no
    reasoning injection.
    """
    frames = _openai_sse(
        [
            {
                "id": "chatcmpl-t",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-t",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
    )
    text = _stream_convert(frames)
    # Exactly one text block opened (the `event:` header marks an event boundary).
    assert text.count("event: content_block_start") == 1
    assert "text_delta" in text
    assert "Hello" in text
    assert "reasoning_content" not in text
