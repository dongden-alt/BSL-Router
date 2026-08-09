"""BUG L regression: only RENDERABLE content may disable combo fallback.

The failure these tests lock down, verbatim from app.out.log:

    [STREAM-GUARD] pix4k/claude-opus-4.8-thinking: refused 1 post-emission
    fallback(s) after 0B [stream_stall] - emitting terminal frame instead of
    a second stream
    [BSL] END status=504 ... ttft=18673.45ms total=91816.94ms in=114406 out=0

"post-emission ... after 0B" is a contradiction: the gate believed content had
been delivered while counting zero bytes. Cause: main.py called
`_emit.mark_emitted()` for EVERY non-None frame from openai_chunk_to_gemini,
but that function returns non-None for frames carrying no renderable content
(usage-only chunks, and finish chunks whose tool calls were dropped, both of
which emit `parts=[{"text": ""}]`). One such early frame permanently disabled
fallback, so a later zero-output stall could not fail over and the IDE hung.
"""

import pytest

from app.compat.adapters.gemini import (
    gemini_frame_has_content,
    openai_chunk_to_gemini,
    terminal_error_frame,
)
from app.middleware.stream_guard import StreamEmissionState


def _wrap(obj):
    return obj if "response" in obj else {"response": obj}


class TestFrameContentDetection:
    """gemini_frame_has_content must track RENDERABILITY, not frame presence."""

    def test_usage_only_chunk_is_not_content(self):
        # The exact frame that poisoned the gate: no choices, usage present.
        # openai_chunk_to_gemini returns parts=[{"text": ""}] to carry usage.
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "usage": {"prompt_tokens": 114406, "completion_tokens": 0}},
            state,
        )
        assert frame is not None, "usage-only chunk must still be forwarded"
        assert gemini_frame_has_content(frame) is False, (
            "usage metadata renders nothing; treating it as content is BUG L"
        )

    def test_empty_text_delta_is_not_content(self):
        assert gemini_frame_has_content(
            {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]}}]}}
        ) is False

    def test_finish_with_no_parts_is_not_content(self):
        # Section 8.8 forces parts=[{"text": ""}] on a finish with nothing to
        # show. That is a stream ending, not rendered output.
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]},
            state,
        )
        assert frame is not None
        assert gemini_frame_has_content(frame) is False

    def test_finish_reason_alone_does_not_count_as_content(self):
        # Deliberate: a finishReason ends the stream but renders nothing. If it
        # counted, fallback would stay blocked on exactly the zero-output
        # finishes we must fail over from.
        assert gemini_frame_has_content(
            {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]},
                                          "finishReason": "STOP"}]}}
        ) is False

    def test_real_text_is_content(self):
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"content": "Hello"}}]}, state
        )
        assert gemini_frame_has_content(frame) is True

    def test_reasoning_text_is_content(self):
        # Thoughts render in the IDE's reasoning pane, so they are corruptible.
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": "thinking"}}]},
            state,
        )
        assert gemini_frame_has_content(frame) is True

    def test_tool_call_is_content(self):
        state = {}
        openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "t1", "function": {"name": "read_file", "arguments": '{"p":1}'}}
            ]}}]},
            state,
        )
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            state,
        )
        assert gemini_frame_has_content(frame) is True

    @pytest.mark.parametrize("junk", [None, {}, "string", 42, {"response": None}, {"response": {}}])
    def test_malformed_input_is_never_content(self, junk):
        # Must never raise: this runs inside the hot streaming loop, and an
        # exception here would kill the stream it is meant to protect.
        assert gemini_frame_has_content(junk) is False

    def test_terminal_error_frame_is_content(self):
        # It carries visible error text, so it is legitimately renderable.
        assert gemini_frame_has_content(terminal_error_frame(504, "stream_stall", "m")) is True


class TestFallbackGateRemainsOpen:
    """The end-to-end invariant: zero rendered output => fallback stays legal."""

    def test_usage_only_frame_does_not_block_fallback(self):
        emit = StreamEmissionState()
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "usage": {"prompt_tokens": 114406, "completion_tokens": 0}},
            state,
        )
        # Mirror the fixed call site.
        if gemini_frame_has_content(frame):
            emit.mark_emitted(b"x")
        assert emit.may_fallback("stream_stall") is True, (
            "this is the 504 freeze: a usage-only frame must not veto fallback"
        )
        assert emit.refused_fallbacks == 0

    def test_real_content_does_block_fallback(self):
        # The gate must still work: after rendered output, failover would
        # corrupt the transcript.
        emit = StreamEmissionState()
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"content": "Hi"}}]}, state
        )
        if gemini_frame_has_content(frame):
            emit.mark_emitted(b'data: {"x":1}\n\n')
        assert emit.may_fallback("stream_stall") is False
        assert emit.refused_fallbacks == 1

    def test_byte_count_stays_consistent_with_emitted_flag(self):
        # The "after 0B" contradiction itself: emitted=True with byte_count=0
        # was only reachable because mark_emitted() was called with no payload.
        emit = StreamEmissionState()
        emit.mark_emitted(b'data: {"response":{}}\n\n')
        assert emit.emitted is True
        assert emit.byte_count > 0, "emitted implies bytes; 0B means the gate lied"

    def test_stall_after_only_usage_frames_can_fail_over(self):
        # Full shape of the logged request: prompt tokens counted, TTFT
        # recorded, zero output, then a stall.
        emit = StreamEmissionState()
        state = {}
        for chunk in (
            {"id": "c1", "model": "m", "usage": {"prompt_tokens": 114406, "completion_tokens": 0}},
            {"id": "c1", "model": "m", "choices": [{"delta": {"content": ""}}]},
        ):
            frame = openai_chunk_to_gemini(chunk, state)
            if frame is not None and gemini_frame_has_content(frame):
                emit.mark_emitted(b"x")
        assert emit.may_fallback("stream_stall") is True
