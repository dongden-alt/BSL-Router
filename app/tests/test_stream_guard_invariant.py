"""Tests for the pre-first-byte fallback invariant.

THE INVARIANT UNDER TEST
    no byte sent to client yet  ->  combo fallback is SAFE
    any byte already sent       ->  combo fallback is FORBIDDEN, permanently

WHY IT MATTERS
Every IDE freeze in this project traces to one operation: abandoning an
in-flight upstream response and starting a different one while the client's SSE
parser is mid-stream. The parser then waits forever for an end it can recognise.

These are pure-logic tests. No server, no network, no mocking of main.py.
"""

import pytest

from app.middleware.stream_guard import StreamEmissionState, may_fallback


def test_pre_emission_fallback_allowed():
    """Before any byte is sent, advancing the combo chain is safe."""
    state = StreamEmissionState()
    assert state.may_fallback() is True
    assert state.emitted is False
    assert state.refused_fallbacks == 0


def test_post_emission_fallback_refused():
    """Once a byte is sent, fallback must be refused."""
    state = StreamEmissionState()
    state.mark_emitted(b"x")
    assert state.emitted is True
    assert state.may_fallback("stream_stall") is False


def test_emission_is_irreversible():
    """Emission can never be undone — the client's parser cannot be un-fed.

    Guards against any future 'reset between chain entries' logic, which would
    silently re-open the freeze.
    """
    state = StreamEmissionState()
    state.mark_emitted(b"first")
    for i in range(5):
        assert state.may_fallback(f"attempt_{i}") is False
    assert state.emitted is True
    assert state.refused_fallbacks == 5


def test_refusal_count_and_reasons_recorded():
    """Refusals are counted and their reasons retained for diagnostics."""
    state = StreamEmissionState()
    state.mark_emitted(b"content")
    state.may_fallback("zero_output_tokens")
    state.may_fallback("stream_stall")
    assert state.refused_fallbacks == 2
    assert "zero_output_tokens" in state.refusal_reasons
    assert "stream_stall" in state.refusal_reasons


def test_refusal_log_empty_when_no_refusals():
    """No refusals -> no log line. Refusals -> an informative one."""
    state = StreamEmissionState()
    assert state.refusal_log() == ""

    # Emission alone is not a refusal.
    state.mark_emitted(b"data")
    assert state.refusal_log() == ""

    state.may_fallback("stream_stall")
    line = state.refusal_log(model="glm-5.2", provider="zhipu")
    assert line != ""
    assert "STREAM-GUARD" in line
    assert "zhipu/glm-5.2" in line
    assert "stream_stall" in line


def test_byte_count_accumulates():
    """Byte count tracks emitted volume for diagnostics."""
    state = StreamEmissionState()
    state.mark_emitted(b"12345")
    state.mark_emitted(b"678")
    assert state.byte_count == 8


def test_mark_emitted_without_chunk_still_marks():
    """A chunk argument is optional; the emitted flag is what matters."""
    state = StreamEmissionState()
    state.mark_emitted()
    assert state.emitted is True
    assert state.may_fallback("x") is False


def test_module_level_guard_tolerates_none():
    """An unmigrated call site (no state threaded) keeps working.

    Migration is incremental; a missing state must not silently disable
    fallback at sites that have not been converted yet.
    """
    assert may_fallback(None) is True
    assert may_fallback(None, "any_reason") is True


def test_module_level_guard_delegates_to_state():
    """With a state supplied, the module-level helper matches the method."""
    state = StreamEmissionState()
    assert may_fallback(state, "pre") is True
    state.mark_emitted(b"x")
    assert may_fallback(state, "post") is False


def test_zero_token_fallback_refused_after_content():
    """REGRESSION: the confirmed bug at app/main.py egress_stream().

    Sequence that used to freeze the IDE:
      1. upstream streams real content; `yield chunk` sends it to the client
      2. the provider omits usage data, so stats["out"] stays 0
      3. the zero-output-token check fires and raises _ComboFallbackNeeded
      4. the router starts a SECOND SSE stream into a parser already mid-way
         through the first -> the IDE waits forever

    Step 3 must now be refused. This must never regress.
    """
    state = StreamEmissionState()
    # Real content reached the client...
    state.mark_emitted(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
    # ...so a zero-output-token fallback is no longer permitted.
    assert state.may_fallback("zero_output_tokens") is False
    assert state.refused_fallbacks == 1
    assert "zero_output_tokens" in state.refusal_reasons


def test_zero_token_fallback_allowed_before_content():
    """Complement: a genuinely empty response may still advance the chain.

    Without this, the fix would break real failover — a dead leaf that produced
    nothing at all SHOULD fall through to the next combo entry.
    """
    state = StreamEmissionState()
    assert state.may_fallback("zero_output_tokens") is True
    assert state.refused_fallbacks == 0


# ---------------------------------------------------------------------------
# Phase 2: the remaining three generators (raw_upstream, gemini, anthropic->openai)
# ---------------------------------------------------------------------------


def test_prefill_before_upstream_blocks_fallback():
    """REGRESSION: raw_upstream's blacksand-chat prefill.

    raw_upstream yields a "Routing payload..." prefill BEFORE contacting
    upstream. Its non-200 handler then wanted to advance the combo chain -- but
    a byte had already reached the client, so that fallback would splice.

    Unlike the other generators, this one is post-emission even on its
    error-status path, which is why it needed the guard most.
    """
    state = StreamEmissionState()
    state.mark_emitted(b'data: {"choices":[{"delta":{"reasoning_content":"Routing..."}}]}\n\n')
    assert state.may_fallback("upstream_502") is False


def test_sse_comments_must_not_count_as_emission():
    """Gemini yields ": heartbeat" / ": keepalive" -- SSE COMMENTS.

    Per the SSE spec a line starting with ":" is a comment and is discarded by
    the parser. It carries no message the client could be mid-way through, so it
    must NOT count as emission -- otherwise every Gemini request would lose
    failover the moment its first keepalive went out.

    This test encodes that decision: the caller is responsible for marking only
    client-visible content, and this is the case that makes it matter.
    """
    state = StreamEmissionState()
    # A heartbeat was sent, but mark_emitted() was deliberately NOT called.
    assert state.may_fallback("upstream_header_timeout") is True
    # Real content is what closes the door.
    state.mark_emitted(b'data: {"response":{"candidates":[...]}}\n\n')
    assert state.may_fallback("upstream_header_timeout") is False


def test_helper_style_guard_returns_rather_than_raises():
    """Models _raise_gemini_combo_fallback's contract.

    That helper is called from 6 sites and raises to advance the chain. When
    refused it must RETURN so the caller falls through to its terminal
    error+DONE frame. Returning is what keeps the client unblocked; raising
    past emission is what froze it.
    """
    state = StreamEmissionState()
    state.mark_emitted(b"content")

    advanced = False
    if state.may_fallback("zero_output_tokens"):
        advanced = True  # would raise _ComboFallbackNeeded here

    assert advanced is False
    assert state.refused_fallbacks == 1


def test_independent_states_do_not_leak():
    """Concurrent requests must not share emission state.

    Each generator call constructs its own instance. If state leaked between
    them, one client's first byte would disable failover for everyone else.
    """
    a, b = StreamEmissionState(), StreamEmissionState()
    a.mark_emitted(b"only a emitted")
    assert a.may_fallback("x") is False
    assert b.may_fallback("x") is True
    assert b.refused_fallbacks == 0


def test_all_four_generator_shapes_share_one_rule():
    """The four client-facing generators differ in shape but obey one rule.

    Shapes: raw_upstream (prefill first), egress_stream (plain), gemini
    (comments then content), anthropic->openai (normalizer). Whatever the
    shape, the question "may I fall back?" has exactly one answer source.
    """
    for _shape in ("raw_upstream", "egress_stream", "gemini", "anthropic_to_openai"):
        s = StreamEmissionState()
        assert s.may_fallback("pre") is True, _shape
        s.mark_emitted(b"first byte")
        assert s.may_fallback("post") is False, _shape
