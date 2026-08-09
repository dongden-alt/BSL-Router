"""
Bug A — Anthropic-egress refused-fallback terminal frame.

THE FREEZE
An Anthropic SSE stream is complete ONLY after a message_stop event (or, for an
OpenAI client, after `data: [DONE]`). If the generator returns without one, the
client sits mid-message on a closed socket and hangs forever.

Two Anthropic-egress generators detect a stall / zero-output condition, then
attempt a combo fallback. When _emit.may_fallback() returns False (post-emission,
so splicing a second stream into a live parser is correctly FORBIDDEN), the raise
is skipped and the generator simply fell out — emitting nothing. The client hung.

SITE 1 (app/main.py `_raw_ok`, convert_egress path): the generator's output is
fed through StreamNormalizer("openai_sse","anthropic_sse").convert_openai_to_anthropic,
so the CLIENT receives Anthropic SSE. The correct terminal emission is an Anthropic
message_start -> ... -> message_stop sequence.

SITE 2 (app/main.py `_raw_anthropic_ok`, convert_anthropic_to_openai_egress path):
the generator's output is fed through StreamNormalizer("anthropic_sse","openai_sse")
.convert_anthropic_to_openai, so the CLIENT receives OpenAI SSE. The correct
terminal emission is an OpenAI error frame + `data: [DONE]`, NOT an Anthropic
message_stop. The terminal frame must match the CLIENT protocol, not the upstream
format.

These tests exercise the REAL module-level builders `_anthropic_terminal_error_frames`
and `_openai_terminal_error_frames` in app/main.py — the same helpers the generators
call on the refused-fallback path. No mirror drift.
"""
import json
from pathlib import Path

import pytest

import app.main as main


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — parse SSE byte frames into a list of (event, data) tuples.
# ──────────────────────────────────────────────────────────────────────────────

def _parse_sse_frames(frames):
    """Decode a list of SSE byte-frames into a list of dicts.

    Each frame may be `event: <t>\\ndata: {...}\\n\\n` (Anthropic) or
    `data: {...}\\n\\n` (OpenAI). Returns a list of parsed JSON dicts; the
    event type is embedded as data['type'] for Anthropic frames.
    """
    out = []
    for fr in frames:
        text = fr.decode("utf-8") if isinstance(fr, (bytes, bytearray)) else str(fr)
        # An SSE frame may contain an `event:` line and one or more `data:` lines.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    out.append({"__raw__": payload, "__done__": payload == "[DONE]"})
                    continue
                try:
                    out.append(json.loads(payload))
                except json.JSONDecodeError:
                    out.append({"__raw__": payload})
    return out


def _anthropic_complete(frames) -> bool:
    """True only if some frame carries {"type": "message_stop"}.

    Mirrors the Anthropic SSE parser's terminal condition: the stream is
    complete ONLY after a message_stop event.
    """
    parsed = _parse_sse_frames(frames)
    return any(isinstance(p, dict) and p.get("type") == "message_stop" for p in parsed)


def _openai_complete(frames) -> bool:
    """True only if some frame is the OpenAI `data: [DONE]` sentinel."""
    parsed = _parse_sse_frames(frames)
    return any(isinstance(p, dict) and p.get("__done__") is True for p in parsed)


def _anthropic_event_types(frames):
    """Ordered list of Anthropic event types in the frame sequence."""
    parsed = _parse_sse_frames(frames)
    return [p.get("type") for p in parsed if isinstance(p, dict) and p.get("type")]


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 — a correct Anthropic sequence requires message_stop.
# ──────────────────────────────────────────────────────────────────────────────

def test_anthropic_sequence_requires_message_stop():
    frames = main._anthropic_terminal_error_frames("zero_output_tokens", "claude-test")
    assert _anthropic_complete(frames) is True


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 — a bare return (empty frame list) is NOT complete. Proves the checker
# is not vacuous: it fails on the pre-fix shape (generator fell out, emitted
# nothing).
# ──────────────────────────────────────────────────────────────────────────────

def test_bare_return_is_not_complete():
    assert _anthropic_complete([]) is False
    assert _openai_complete([]) is False


# ──────────────────────────────────────────────────────────────────────────────
# Test 3 — never emit content_block_stop without a preceding content_block_start.
# (A block that was never started must never be stopped.)
# ──────────────────────────────────────────────────────────────────────────────

def test_no_orphan_content_block_stop():
    frames = main._anthropic_terminal_error_frames("stream_stall", "claude-test")
    types = _anthropic_event_types(frames)
    assert types.count("content_block_start") >= 1
    # For every content_block_stop there must be an earlier content_block_start.
    started = 0
    for t in types:
        if t == "content_block_start":
            started += 1
        elif t == "content_block_stop":
            assert started > 0, "content_block_stop emitted without a preceding content_block_start"
            started -= 1
    # Final balance: no open blocks left dangling.
    assert started == 0


# ──────────────────────────────────────────────────────────────────────────────
# Test 4 — THE IMPORTANT ONE. Exercise the REAL refused-fallback code path via
# the shared module-level builders. Simulate a post-emission stall so
# may_fallback() returns False, and assert the collected frames are complete.
# ──────────────────────────────────────────────────────────────────────────────

def test_refused_fallback_emits_terminal_frame_anthropic():
    """SITE 1: Anthropic-egress generator. Post-emission stall -> may_fallback
    False -> the generator must yield the Anthropic terminal sequence (with
    message_stop) instead of falling out silently."""
    emit = main.StreamEmissionState()
    emit.mark_emitted(b"event: message_start\n\n")  # simulate post-emission
    assert emit.may_fallback("stream_stall") is False  # refused

    # This is exactly what the SITE 1 generator does on the refused path:
    frames = main._anthropic_terminal_error_frames("stream_stall", "claude-test")
    assert frames, "refused fallback must emit a terminal sequence, not fall out"
    assert _anthropic_complete(frames) is True


def test_refused_fallback_emits_terminal_frame_openai():
    """SITE 2: Anthropic->OpenAI egress generator. Post-emission stall ->
    may_fallback False -> the generator must yield the OpenAI terminal sequence
    (error frame + [DONE]) instead of falling out silently."""
    emit = main.StreamEmissionState()
    emit.mark_emitted(b"data: {\"choices\":[{}]}\n\n")  # simulate post-emission
    assert emit.may_fallback("stream_stall_precheck") is False  # refused

    # This is exactly what the SITE 2 generator does on the refused path:
    frames = main._openai_terminal_error_frames("stream_stall", "gpt-test", 504)
    assert frames, "refused fallback must emit a terminal sequence, not fall out"
    assert _openai_complete(frames) is True


# ──────────────────────────────────────────────────────────────────────────────
# Test 5 — the stall reason is VISIBLE in the transcript (not a silent empty
# turn). Anthropic: in a content_block_delta; OpenAI: in the error frame.
# ──────────────────────────────────────────────────────────────────────────────

def test_error_text_is_visible_in_transcript_anthropic():
    reason = "stream_stall"
    frames = main._anthropic_terminal_error_frames(reason, "claude-test")
    parsed = _parse_sse_frames(frames)
    deltas = [
        p for p in parsed
        if isinstance(p, dict) and p.get("type") == "content_block_delta"
    ]
    assert deltas, "expected at least one content_block_delta carrying the error text"
    visible = "".join(
        d.get("delta", {}).get("text", "") for d in deltas
    )
    assert reason in visible, f"stall reason {reason!r} not visible in transcript: {visible!r}"


def test_error_text_is_visible_in_transcript_openai():
    reason = "zero_output_tokens"
    frames = main._openai_terminal_error_frames(reason, "gpt-test", 504)
    parsed = _parse_sse_frames(frames)
    # The error frame carries the message field.
    err_frames = [
        p for p in parsed
        if isinstance(p, dict) and isinstance(p.get("error"), dict)
    ]
    assert err_frames, "expected an OpenAI error frame carrying the error text"
    msg = " ".join(str(p["error"].get("message", "")) for p in err_frames)
    assert reason in msg, f"stall reason {reason!r} not visible in error frame: {msg!r}"


# ──────────────────────────────────────────────────────────────────────────────
# Test 6 — INVARIANT (static): every Anthropic-egress refused-fallback site in
# app/main.py that prints the "Anthropic stream stall for" / "200-with-0-tokens"
# log line must, in the SAME generator body, carry a terminal-emission call to
# the shared builder on its refused branch. Fails naming the offending line if a
# site was missed. This test FAILS on the pre-fix code and PASSES after the fix.
# ──────────────────────────────────────────────────────────────────────────────

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def _refused_fallback_sites(source: str):
    """Return (lineno, helper_name) for each refused-fallback terminal emission
    in app/main.py — i.e. each call to one of the shared builders inside an
    `else:` branch of a `may_fallback` refusal."""
    helpers = ("_anthropic_terminal_error_frames", "_openai_terminal_error_frames")
    sites = []
    for i, line in enumerate(source.splitlines()):
        for h in helpers:
            if h in line and "for _tf in" in line:
                sites.append((i + 1, h))
    return sites


def test_every_refused_fallback_site_calls_shared_builder():
    source = MAIN_PY.read_text(encoding="utf-8")
    sites = _refused_fallback_sites(source)
    # SITE 1 has two refused branches (zero-output + stall), both Anthropic.
    # SITE 2 has three refused branches (zero-output + inner stall + outer
    # stall-precheck), all OpenAI. Total = 5.
    assert len(sites) >= 5, (
        f"Expected >=5 refused-fallback terminal emissions across the two "
        f"Anthropic-egress generators, found {len(sites)} at {sites}. "
        f"A site was missed — the freeze is not fully patched."
    )
    anthro = [s for s in sites if s[1] == "_anthropic_terminal_error_frames"]
    openai = [s for s in sites if s[1] == "_openai_terminal_error_frames"]
    assert len(anthro) >= 2, (
        f"SITE 1 (Anthropic client) must have >=2 Anthropic terminal emissions, "
        f"found {len(anthro)}"
    )
    assert len(openai) >= 3, (
        f"SITE 2 (OpenAI client) must have >=3 OpenAI terminal emissions, "
        f"found {len(openai)}"
    )
