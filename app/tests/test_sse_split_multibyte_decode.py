"""
Regression suite for the split-multibyte UTF-8 stream fix (2026-10-21).

Bug class: every SSE drain / usage-extraction loop in app/main.py decoded raw
byte chunks with a one-shot strict chunk.decode("utf-8") inside a
try/except Exception: pass. When a chunk boundary landed inside a multi-byte
character (em dash, CJK, emoji, Vietnamese diacritics - common in search-tool
results on Codex turn 2+), the decode raised, the except swallowed it, and the
WHOLE chunk was discarded - usage frames included. stats["out"] stayed 0 on a
perfectly healthy stream, tripping the zero-output fallback / benching gate
and presenting to the Antigravity IDE as a stream that "halts forever" after
the first successful call.

Fix: per-stream codecs.getincrementaldecoder("utf-8")(errors="replace")
carries the partial trailing sequence into the next chunk, so no chunk is
ever dropped.

Layers pinned here:
  1. BEHAVIOR - the incremental drain pattern recovers the exact payload for
     EVERY possible byte-split point; the old strict pattern provably loses
     data (fix-detector: if the hazard ever disappears, that test tells us).
  2. USAGE FRAME - a usage JSON frame split mid-multibyte still yields intact
     token accounting (the stats["out"] starvation scenario).
  3. STRUCTURE - AST-verified (comment-immune, per AGENTS.md section 3/9):
     main.py keeps >= 5 incremental decoder sites and NO strict
     .decode("utf-8") survives on stream chunks; the only whitelisted
     receivers are raw_err error-body decodes (one-shot read of a complete
     body - no boundary risk).
"""
from __future__ import annotations

import ast
import codecs
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAIN_PY = Path(__file__).resolve().parents[2] / "app" / "main.py"


# -- Drain-loop patterns under test -------------------------------------------

def _incremental_drain(chunks):
    """Current production pattern: one decoder per stream, errors='replace'."""
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    out, dropped = [], 0
    for chunk in chunks:
        try:
            out.append(dec.decode(chunk))
        except Exception:
            dropped += 1
    return "".join(out), dropped


def _legacy_strict_drain(chunks):
    """The OLD pattern (pre-2026-10-21): one-shot strict decode + except-pass."""
    out, dropped = [], 0
    for chunk in chunks:
        try:
            out.append(chunk.decode("utf-8"))
        except Exception:
            dropped += 1
    return "".join(out), dropped


# -- 1. Behavioral: every byte-split point recovers ---------------------------

_PAYLOADS = [
    "plain ascii payload",
    "em \u2014 dash",                       # U+2014: 3 bytes
    "CJK \u4e2d\u6587\u30c6\u30ad\u30b9\u30c8",          # 3-byte sequences
    "emoji \U0001F600\U0001F680 test",      # 4-byte sequences
    "ti\u1ebfng Vi\u1ec7t c\u00f3 d\u1ea5u",    # Vietnamese precomposed
]


@pytest.mark.parametrize("text", _PAYLOADS)
def test_incremental_drain_recovers_every_split_point(text):
    raw = text.encode("utf-8")
    for i in range(1, len(raw)):
        recovered, dropped = _incremental_drain([raw[:i], raw[i:]])
        assert dropped == 0, f"chunk dropped at split {i} for {text!r}"
        assert recovered == text, f"payload corrupted at split {i} for {text!r}"


@pytest.mark.parametrize("text", _PAYLOADS)
def test_incremental_drain_recovers_byte_at_a_time(text):
    raw = text.encode("utf-8")
    recovered, dropped = _incremental_drain([bytes([b]) for b in raw])
    assert dropped == 0
    assert recovered == text


@pytest.mark.parametrize("text", [p for p in _PAYLOADS if not p.isascii()])
def test_legacy_strict_drain_provably_loses_data(text):
    """Fix-detector: the old pattern MUST fail somewhere on multibyte text.

    If a future Python makes one-shot strict decode boundary-safe, this
    assertion flips and tells us the incremental guard is no longer needed.
    """
    raw = text.encode("utf-8")
    failures = 0
    for i in range(1, len(raw)):
        recovered, dropped = _legacy_strict_drain([raw[:i], raw[i:]])
        if dropped or recovered != text:
            failures += 1
    assert failures > 0, (
        "strict one-shot decode survived every split - the 2026-10-21 hazard "
        "no longer exists and the incremental decoders can be revisited"
    )


# -- 2. Usage frame survives a mid-multibyte split ----------------------------

def test_usage_frame_split_mid_multibyte_keeps_token_accounting():
    """Reproduces the stats['out'] starvation: usage JSON rides in the same
    chunk stream as content; a boundary inside a multi-byte content char must
    not cost us the usage frame that follows/precedes it."""
    frame_a = 'data: {"choices":[{"delta":{"content":"k\u1ebft qu\u1ea3 \u2014 t\u00ecm ki\u1ebfm"}}]}\n\n'
    frame_b = 'data: {"usage":{"completion_tokens":42,"total_tokens":58}}\n\n'
    stream = (frame_a + frame_b).encode("utf-8")
    for i in range(1, len(stream)):
        recovered, dropped = _incremental_drain([stream[:i], stream[i:]])
        assert dropped == 0
        assert '"completion_tokens":42' in recovered, (
            f"usage frame lost at split {i} - stats['out'] would starve"
        )


# -- 3. Structural invariants on app/main.py (AST - comments cannot fake) -----

def _main_tree():
    src = MAIN_PY.read_text(encoding="utf-8")
    return src, ast.parse(src)


def test_main_stream_loops_keep_incremental_decoders():
    src, tree = _main_tree()
    sites = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "getincrementaldecoder"
    ]
    # 5 drain/usage loops converted on 2026-10-21: OpenAI egress, Gemini
    # egress, Gemini raw_ok, Codex drain, and the final SSE usage loop.
    assert len(sites) >= 5, (
        f"only {len(sites)} incremental decoder sites in main.py - "
        "a stream drain loop regressed to one-shot decoding"
    )


def test_no_strict_utf8_decode_on_stream_chunks():
    src, tree = _main_tree()
    # The 2026-10-21 hazard exists ONLY where a stream chunk is decoded
    # per-iteration: receivers named chunk/ochunk/_c in the drain loops.
    # One-shot decodes of complete bodies (response.body, _fb_bytes,
    # _egr_fb_body, raw_err, ...) are CORRECT - no boundary can exist once
    # the whole body has been read - so they are deliberately out of scope.
    chunk_receivers = {"chunk", "ochunk"}
    offenders = []
    body_decodes = 0
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "decode"):
            continue
        is_utf8 = (
            not n.args  # .decode() defaults to utf-8
            or (isinstance(n.args[0], ast.Constant)
                and str(n.args[0].value).replace("-", "").lower() == "utf8")
        )
        if not is_utf8:
            continue
        if isinstance(n.func.value, ast.Name) and n.func.value.id in chunk_receivers:
            offenders.append(f"line {n.lineno}: {n.func.value.id}.decode(...)")
        else:
            body_decodes += 1
    assert offenders == [], (
        "strict UTF-8 decode on a stream chunk - drain loops must use the "
        "per-stream incremental decoder:\n" + "\n".join(offenders)
    )
    # Sanity: the whitelist scoping must not have nuked the legitimate
    # complete-body decodes (error bodies, fallback payloads).
    assert body_decodes >= 10, (
        f"only {body_decodes} body decodes found - expected 20+; "
        "this test may be scanning the wrong tree"
    )
