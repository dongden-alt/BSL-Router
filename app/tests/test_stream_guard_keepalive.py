"""BUG M (2026-09-27) regression: SSE keepalive/comment-only chunks must NOT
commit the stream transcript, and reasoning/thinking deltas must feed the
AntiStop continuation detector.

Two coupled defects produced the persistent client-visible 502
`midstream_transport` errors on slow reasoning streams:

1.  EMISSION-GATE POISON. Providers emit SSE comment keepalives
    (`: PING\\n\\n`) during slow TTFT. The raw pump paths called
    `mark_emitted(chunk)` unconditionally, so an 8-byte keepalive flipped
    `emitted=True` and `may_fallback()` vetoed every later fallback. When the
    leaf then died mid-stream with zero content delivered, the gate refused
    the failover and the client got a terminal 502. Logged shape, verbatim:

        [STREAM-GUARD] vsllm-o/glm-5.2: refused 1 post-emission fallback(s)
        after 8B [midstream_transport_502] | first-bytes: b': PING\\n\\n'

    Fix: raw pump paths consult `is_scaffolding_only` before `mark_emitted`.

2.  DEAD ANTISTOP CONTINUATION. `StreamTruncationDetector._observe` only
    accumulated body `content`/`text` deltas, never `reasoning_content` /
    `thinking` / Gemini thought parts, so on thinking-heavy streams
    `partial_text` stayed empty and `should_splice_continuation` refused the
    transport-death continuation on its `not partial_text.strip()` gate --
    a guaranteed terminal 502 instead of a same-provider resume.

These tests lock both invariants. Pure-logic, no server, no network.
"""

from app.middleware.stream_guard import StreamEmissionState, is_scaffolding_only
from app.middleware.quality import StreamTruncationDetector, should_splice_continuation


# --- Defect 1: keepalive / scaffolding must not commit the transcript -------


def test_ping_keepalive_does_not_commit_transcript():
    """The exact logged poison: a bare `: PING` SSE comment must not flip
    `emitted`. If it did, the post-death fallback that would have saved the
    request is vetoed and the client 502s with zero content delivered."""
    emit = StreamEmissionState()
    chunk = b": PING\n\n"
    # Mirror the fixed raw-pump call site.
    if not is_scaffolding_only(chunk):
        emit.mark_emitted(chunk)
    assert emit.emitted is False, "a PING keepalive must never commit the transcript"
    assert emit.may_fallback("midstream_transport") is True
    assert emit.refused_fallbacks == 0


def test_named_keepalive_and_heartbeat_do_not_commit():
    for chunk in (b": keepalive\n\n", b": ka\n\n", b":\n\n", b": hb\n\n"):
        emit = StreamEmissionState()
        if not is_scaffolding_only(chunk):
            emit.mark_emitted(chunk)
        assert emit.may_fallback("midstream_transport") is True, chunk


def test_real_content_still_commits_transcript():
    """The invariant the guard exists for: once renderable content reaches the
    client, fallback stays forbidden (transcript integrity)."""
    emit = StreamEmissionState()
    chunk = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    if not is_scaffolding_only(chunk):
        emit.mark_emitted(chunk)
    assert emit.emitted is True
    assert emit.may_fallback("midstream_transport") is False
    assert emit.refused_fallbacks == 1


def test_mixed_keepalive_and_content_chunk_commits():
    """A chunk that batches a comment line WITH a content frame carries real
    output -- it must commit (fail-safe direction)."""
    emit = StreamEmissionState()
    chunk = b': PING\ndata: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    if not is_scaffolding_only(chunk):
        emit.mark_emitted(chunk)
    assert emit.may_fallback("midstream_transport") is False


def test_ambiguous_chunk_fails_open_to_content():
    """Fail-open contract: anything the detector cannot prove is pure
    scaffolding counts as content (safer -- never re-opens the freeze)."""
    emit = StreamEmissionState()
    chunk = b'data: {"some":"unrecognised frame"}\n\n'
    if not is_scaffolding_only(chunk):
        emit.mark_emitted(chunk)
    # Unrecognised -> treated as content -> committed -> fallback refused.
    assert emit.may_fallback("midstream_transport") is False


def test_full_ping_then_death_sequence_recovers():
    """End-to-end shape of the logged failure: PING keepalive(s), then a
    transport death with zero content. Fallback must STILL be legal."""
    emit = StreamEmissionState()
    for chunk in (b": PING\n\n", b": PING\n\n", b": keepalive\n\n"):
        if not is_scaffolding_only(chunk):
            emit.mark_emitted(chunk)
    # Leaf now dies mid-stream with no content -- fallback must be available.
    assert emit.may_fallback("midstream_transport") is True


# --- Defect 2: reasoning/thinking deltas feed the continuation detector -----


def test_openai_reasoning_content_accumulates_for_continuation():
    det = StreamTruncationDetector()
    det.feed(b'data: {"choices":[{"delta":{"reasoning_content":"thinking step 1"}}]}\n\n')
    det.feed(b'data: {"choices":[{"delta":{"reasoning_content":" and step 2"}}]}\n\n')
    assert "thinking step 1" in det.partial_text
    assert "and step 2" in det.partial_text


def test_anthropic_thinking_delta_accumulates():
    det = StreamTruncationDetector()
    det.feed(b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"reasoning..."}}\n\n')
    assert "reasoning..." in det.partial_text


def test_gemini_thought_part_accumulates():
    det = StreamTruncationDetector()
    det.feed(b'data: {"candidates":[{"content":{"parts":[{"text":"chain of thought","thought":true}]}}]}\n\n')
    assert "chain of thought" in det.partial_text


def test_transport_death_mid_thinking_allows_continuation():
    """The production gap: thinking-only partial output must satisfy the
    continuation gate so a transport death resumes instead of 502ing."""
    det = StreamTruncationDetector()
    # Only reasoning flowed before the socket died (no body content yet).
    det.feed(b'data: {"choices":[{"delta":{"reasoning_content":"let me work through this"}}]}\n\n')
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=True,
        partial_text=det.partial_text,
        infinite_retry_enabled=True,
        used=False,
    ) is True


def test_empty_partial_text_still_refuses_continuation():
    """A dead leaf that produced NOTHING must not splice -- terminal is
    correct there. The reasoning fix must not reopen this."""
    det = StreamTruncationDetector()
    assert should_splice_continuation(
        truncated=False,
        transport_died_partial=True,
        partial_text=det.partial_text,
        infinite_retry_enabled=True,
        used=False,
    ) is False
