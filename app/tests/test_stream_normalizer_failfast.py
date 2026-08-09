"""Tool-input handling for OpenAI→Anthropic stream conversion.

CONTRACT CHANGE (2026-08-04): this file was `fail-fast` -- it asserted that a
bad tool call raised `StreamConversionError`. It no longer does, and the reason
is the whole point of the change.

Nothing in production code ever caught that exception (only these tests did).
So it escaped mid-stream: the turn ended with no `message_stop`, the client was
left holding a half-open tool block, and -- critically -- THE MODEL WAS NEVER
TOLD WHAT WENT WRONG, so it could not fix the call on a retry.

The user-visible tell that isolated this: a model emitting valid-but-incomplete
arguments would see the client's rejection and self-correct, while a model
emitting truncated JSON would get permanently stuck. Same class of model
mistake, opposite outcome, purely because of how WE handled it.

The router now repairs and forwards. The client owns the tool schema; its
rejection is the feedback signal that teaches the model. Our job is to deliver
the call and terminate the stream cleanly, not to adjudicate it.
"""

from __future__ import annotations

import asyncio
import json


async def _collect(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return b"".join(chunks)


def _openai_sse(chunks: list[dict]) -> list[bytes]:
    frames = [f"data: {json.dumps(chunk)}\n\n".encode("utf-8") for chunk in chunks]
    frames.append(b"data: [DONE]\n\n")
    return frames


async def _byte_stream(frames: list[bytes]):
    for frame in frames:
        yield frame


def _tool_call_frames(call_id: str, name: str, arguments: str) -> list[bytes]:
    """A single-tool-call stream whose arguments we control."""
    return _openai_sse(
        [
            {
                "id": f"chatcmpl-{call_id}",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ]
    )


def _convert(frames: list[bytes]) -> str:
    from app.compat.stream_normalizer import StreamNormalizer

    normalizer = StreamNormalizer("openai_sse", "anthropic_sse", model_name="gpt-test")
    out = asyncio.run(_collect(normalizer.convert_openai_to_anthropic(_byte_stream(frames))))
    return out.decode("utf-8")


def test_valid_tool_args_emit_tool_use_blocks():
    frames = _openai_sse(
        [
            {
                "id": "chatcmpl-1",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "model": "gpt-test",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"city":"Hanoi"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "model": "gpt-test",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
    )
    text = _convert(frames)
    assert "content_block_start" in text
    assert "tool_use" in text
    assert "get_weather" in text
    assert "partial_json" in text
    assert "message_stop" in text


def test_malformed_tool_args_are_repaired_not_fatal():
    """Truncated tool JSON must not kill the turn.

    This is the case that left Qwen/DeepSeek unable to recover: the stream died
    before `message_stop`, so the client never reported a usable error and the
    model had nothing to correct against.
    """
    text = _convert(_tool_call_frames("call_bad", "broken", '{"city":'))

    # Load-bearing: the turn completes, so the client can act on it.
    assert "message_stop" in text
    # The call still surfaces, so the client's rejection reaches the model.
    assert "tool_use" in text
    assert "broken" in text
    # The unparseable payload is preserved for diagnosis, not silently dropped.
    assert "_bsl_malformed_arguments" in text


def test_empty_tool_args_are_valid_empty_object():
    """`arguments == ""` is a VALID call to a zero-parameter tool.

    The old code raised "empty JSON object required" and rejected a correct
    call: a tool with no required parameters has nothing to serialize. Anthropic
    wants an object, so `{}` is the right rendering -- exactly what the old
    error message asked for. This was a real source of "cannot call <tool>".
    """
    text = _convert(_tool_call_frames("call_empty", "noop", ""))

    assert "message_stop" in text
    assert "tool_use" in text
    assert "noop" in text
    assert "{}" in text


def test_non_object_tool_args_are_wrapped_not_fatal():
    """A non-object payload is wrapped and forwarded, never fatal."""
    text = _convert(_tool_call_frames("call_arr", "bad", "[1,2]"))

    assert "message_stop" in text
    assert "tool_use" in text
    assert "_bsl_unexpected_arguments" in text


def test_every_bad_tool_call_still_terminates_the_stream():
    """The invariant that ties all of the above together.

    Whatever the model emits, the client must receive a terminated stream. A
    turn that ends without `message_stop` is indistinguishable from a hang.
    """
    for label, args in (
        ("truncated", '{"city":'),
        ("empty", ""),
        ("array", "[1,2]"),
        ("scalar", "42"),
        ("garbage", "not json at all"),
        ("valid", '{"city":"Hanoi"}'),
    ):
        text = _convert(_tool_call_frames(f"call_{label}", "run_command", args))
        assert "message_stop" in text, f"{label!r} failed to terminate the stream"
