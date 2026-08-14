"""BUG N regression: pre-render buffer keeps fallback legal during reasoning.

The failure these tests lock down, verbatim from app.out.log (2026-08-13):

    [502] stream:true GPT-5.6-SOL > qwencoder/gpt-5.6-sol | 78816ms (TTFT
    47368) | In: 0 | Out: 0 | ERROR: midstream_transport: peer closed
    connection ... incomplete chunked read

The stream produced ONLY reasoning (`thought:true`) frames and then the leaf
died mid-transport. `gemini_frame_has_content` correctly counted thought text
as content (it renders in the reasoning pane), so `mark_emitted()` fired on
the first thought, fallback was refused, and the user got a dead stream while
healthy combo entries went untried.

The fix pairs a second predicate with the existing one:
  * gemini_frame_has_content — "would failover corrupt anything rendered?"
    (thoughts included: they render in the reasoning pane)
  * gemini_frame_is_thought_only — "is this frame EXCLUSIVELY reasoning?"
    (thoughts render in the reasoning pane but NOT the transcript body, so
    pre-content thoughts can be silently replayed by a failover)

main.py's Gemini egress holds thought-only frames in a capped buffer while
`emitted` stays False; the first BODY-content frame flushes the buffer and
commits. A pre-commit transport death drops the buffer and advances the combo.
"""

import pytest

from app.compat.adapters.gemini import (
    gemini_frame_has_content,
    gemini_frame_is_thought_only,
    openai_chunk_to_gemini,
)
from app.middleware.stream_guard import StreamEmissionState


def _wrap(obj):
    return obj if "response" in obj else {"response": obj}


class TestThoughtOnlyClassification:
    """gemini_frame_is_thought_only must be TRUE only for pure thought frames."""

    def test_pure_thought_frame_is_thought_only(self):
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": "hmm"}}]},
            state,
        )
        assert frame is not None
        assert gemini_frame_is_thought_only(frame) is True

    def test_mixed_thought_and_text_frame_is_not_thought_only(self):
        # A frame carrying BOTH a thought part and visible text commits.
        frame = {"response": {"candidates": [{"content": {"role": "model", "parts": [
            {"thought": True, "text": "hmm"},
            {"text": "answer"},
        ]}}]}}
        assert gemini_frame_is_thought_only(frame) is False

    def test_visible_text_frame_is_not_thought_only(self):
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"content": "Hello"}}]}, state
        )
        assert gemini_frame_is_thought_only(frame) is False

    def test_tool_call_frame_is_not_thought_only(self):
        frame = {"response": {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "read_file", "args": {}}},
        ]}}]}}
        assert gemini_frame_is_thought_only(frame) is False

    def test_empty_thought_frame_is_not_thought_only(self):
        # Empty thought text is not a thought: conservative — it must pass
        # through unbuffered rather than being held forever.
        frame = {"response": {"candidates": [{"content": {"role": "model", "parts": [
            {"thought": True, "text": ""},
        ]}}]}}
        assert gemini_frame_is_thought_only(frame) is False

    def test_no_parts_frame_is_not_thought_only(self):
        # Usage-only / finish-only frames have no content at all. They are NOT
        # thoughts — they pass through unbuffered and uncommitted.
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "usage": {"prompt_tokens": 100, "completion_tokens": 0}},
            state,
        )
        assert frame is not None
        assert gemini_frame_is_thought_only(frame) is False

    @pytest.mark.parametrize("junk", [None, {}, "string", 42, {"response": None}, {"response": {}}])
    def test_malformed_input_is_never_thought_only(self, junk):
        # Runs inside the hot streaming loop; must never raise.
        assert gemini_frame_is_thought_only(junk) is False

    def test_malformed_part_shape_is_not_thought_only(self):
        frame = {"response": {"candidates": [{"content": {"role": "model", "parts": [42]}}]}}
        assert gemini_frame_is_thought_only(frame) is False

    def test_content_alone_is_not_content_only(self):
        # Invariant pairing: a pure thought frame IS content (renderable in the
        # reasoning pane) but is NOT body content. This is exactly why BUG N
        # needed a second predicate instead of reusing has_content.
        state = {}
        frame = openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": "thinking"}}]},
            state,
        )
        assert gemini_frame_has_content(frame) is True
        assert gemini_frame_is_thought_only(frame) is True


class TestBufferGateSemantics:
    """The egress gate's decision table, mirrored in StreamEmissionState."""

    def _gate(self, frames_bytes):
        """Mirror main.py's per-frame logic. frames_bytes: list of (frame, payload).

        Returns (emit_state, yielded_payloads, buffered_payloads).
        """
        emit = StreamEmissionState()
        emitted = False
        buf = []
        yielded = []
        for frame, payload in frames_bytes:
            is_body = gemini_frame_has_content(frame) and not gemini_frame_is_thought_only(frame)
            if not emitted and not is_body:
                if gemini_frame_is_thought_only(frame):
                    buf.append(payload)
                    continue
                yielded.append(payload)  # scaffolding passes through uncommitted
                continue
            if buf:
                yielded.extend(buf)
                buf.clear()
            if gemini_frame_has_content(frame):
                emitted = True
                emit.mark_emitted(payload)
            yielded.append(payload)
        return emit, yielded, buf

    def test_thought_stream_death_keeps_fallback_legal(self):
        # The exact BUG N shape: thoughts arrive, leaf dies before any body
        # content. The buffer holds the thoughts; the gate never commits.
        state = {}
        frames = []
        for i, text in enumerate(("hmm", "let me think", "analyze")):
            frame = openai_chunk_to_gemini(
                {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": text}}]},
                state,
            )
            frames.append((_wrap(frame), f"data: thought{i}\n\n".encode()))
        emit, yielded, buf = self._gate(frames)
        assert yielded == [], "pre-content thoughts must NOT reach the client"
        assert len(buf) == 3, "thoughts held for silent replay/discard"
        assert emit.emitted is False
        assert emit.may_fallback("midstream_transport") is True, (
            "BUG N: thought-only stream must leave fallback legal for the combo"
        )

    def test_first_body_text_flushes_buffer_and_commits(self):
        state = {}
        thought = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": "hmm"}}]},
            state,
        ))
        body = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"content": "answer"}}]},
            state,
        ))
        emit, yielded, buf = self._gate([
            (thought, b"data: thought\n\n"),
            (body, b"data: body\n\n"),
        ])
        assert yielded == [b"data: thought\n\n", b"data: body\n\n"], (
            "buffered thoughts must flush in order before the committing frame"
        )
        assert buf == []
        assert emit.emitted is True
        assert emit.may_fallback("stream_stall") is False, (
            "body text in transit: failover would corrupt the transcript"
        )

    def test_scaffolding_between_thoughts_does_not_commit(self):
        # usage-only and finish-only frames interleaved with thoughts must pass
        # through WITHOUT committing — otherwise a later transport death would
        # be wrongly blocked.
        state = {}
        thought = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": "hmm"}}]},
            state,
        ))
        usage = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 0}},
            state,
        ))
        finish = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]},
            state,
        ))
        emit, yielded, buf = self._gate([
            (thought, b"data: thought\n\n"),
            (usage, b"data: usage\n\n"),
            (finish, b"data: finish\n\n"),
        ])
        # usage + finish yielded as scaffolding; thought still buffered.
        assert b"data: usage\n\n" in yielded
        assert b"data: finish\n\n" in yielded
        assert buf == [b"data: thought\n\n"], "thought must stay buffered pre-content"
        assert emit.emitted is False
        assert emit.may_fallback("midstream_transport") is True

    def test_zero_body_stream_flushes_buffer_on_completion(self):
        # A thought-only stream that completes with zero body tokens: the
        # held thoughts are surfaced before [DONE] (there is no fallback
        # decision left to protect), and the zero-token fallback gate still
        # fires because `emitted` stayed False.
        state = {}
        thought = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"reasoning_content": "hmm"}}]},
            state,
        ))
        emit, yielded, buf = self._gate([(thought, b"data: thought\n\n")])
        # main.py's final flush before [DONE]:
        if buf:
            yielded.extend(buf)
            buf.clear()
        assert yielded == [b"data: thought\n\n"]
        assert emit.emitted is False, (
            "thoughts surfaced at completion must NOT retroactively commit: "
            "the zero_output_tokens fallback gate depends on emitted staying False"
        )

    def test_body_content_still_blocks_fallback(self):
        # The gate must still work for real content (BUG L invariant preserved).
        emit = StreamEmissionState()
        state = {}
        frame = _wrap(openai_chunk_to_gemini(
            {"id": "c1", "model": "m", "choices": [{"delta": {"content": "Hi"}}]}, state
        ))
        if gemini_frame_has_content(frame) and not gemini_frame_is_thought_only(frame):
            emit.mark_emitted(b'data: {"x":1}\n\n')
        assert emit.may_fallback("stream_stall") is False
        assert emit.refused_fallbacks == 1
