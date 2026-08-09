"""
Gemini SSE error-termination contract — stateful parser model.

THE FREEZE (2026-08-07)
An all-leaves-429 reproduction proved the terminal sequence was:

    1. data: {"error": {"code": 429, ...}}                       (top-level, no candidates)
    2. data: {"response": {"candidates": [{"finishReason": "STOP"}]}}
    3. data: [DONE]

The Antigravity Gemini parser treats a top-level `error` object as an
error/terminal transition: once it consumes frame 1 it stops processing
subsequent `candidates` frames, so the `finishReason` it needs to actually end
the stream is never acted on and the IDE waits on a closed connection. The
`[DONE]` sentinel is discarded by the Gemini wire protocol regardless. The IDE
froze despite frame 2 carrying a finishReason — which is why the earlier
"any later finishReason means success" test was vacuous and missed the bug.

THE CONTRACT (enforced here, statefully)
A Gemini error exit must emit exactly ONE parser-valid terminal data frame:
a `{"response": {"candidates": [{"finishReason": ...}]}}` envelope (produced by
`terminal_error_frame`), optionally followed by a single `data: [DONE]`. It must
NOT be preceded by a top-level `{"error": ...}` frame, and it must NOT emit a
duplicate terminal frame or a duplicate `[DONE]`.

These tests are static + unit — no server, no network.
"""
import json
import re
from pathlib import Path

import pytest

from app.compat.adapters.gemini import (
    terminal_error_frame,
    sse_data as gemini_sse_data,
    SSE_DONE as GEMINI_SSE_DONE,
)


MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


# ──────────────────────────────────────────────────────────────────────────────
# Stateful parser emulation — models Antigravity's per-stream state machine
# ──────────────────────────────────────────────────────────────────────────────

class _GeminiParserState:
    """A faithful-enough model of the Antigravity Gemini SSE parser state.

    The parser advances through `data:` lines and tracks:

      * `saw_top_level_error` — a top-level `{"error": ...}` object (no
        `candidates`) transitions the parser into an ERRORED state from which
        it stops acting on later `candidates` frames. This is the poison.
      * `terminated` — set True ONLY when a `candidates` frame carrying a
        `finishReason` is consumed WHILE the parser is still healthy. A
        finishReason seen after a top-level error does NOT terminate.
      * `done_count` — number of `data: [DONE]` lines (Gemini discards these,
        but duplicates are still a wire defect we forbid).
      * `terminal_candidate_count` — number of finishReason-bearing candidate
        frames consumed while healthy (must be exactly 1).

    `data:` lines that are SSE comments (`: heartbeat`) are ignored, matching
    the Gemini wire (only `data:` payloads carry stream chunks).
    """

    def __init__(self):
        self.saw_top_level_error = False
        self.terminated = False
        self.done_count = 0
        self.terminal_candidate_count = 0
        self.frames = []  # parsed data payloads, in order, for assertions

    def feed_bytes(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        for line in text.splitlines():
            self._feed_line(line.strip())

    def _feed_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return  # SSE comment / event prefix — Gemini ignores
        payload = line[len("data:"):].strip()
        if not payload:
            return
        if payload == "[DONE]":
            self.done_count += 1
            return
        try:
            obj = json.loads(payload)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        self.frames.append(obj)
        candidates = self._candidates_of(obj)
        is_bare_error = isinstance(obj.get("error"), dict) and not candidates
        if is_bare_error:
            # THE POISON: a top-level error object with no candidates flips the
            # parser into an error state. Any later finishReason frame is not
            # acted on, so the stream does not terminate.
            self.saw_top_level_error = True
            return
        if self.saw_top_level_error:
            # Parser already errored — it does not consume further candidates.
            return
        for cand in candidates:
            if isinstance(cand, dict) and cand.get("finishReason"):
                self.terminal_candidate_count += 1
                self.terminated = True

    @staticmethod
    def _candidates_of(obj: dict) -> list:
        out = []
        top = obj.get("candidates")
        if isinstance(top, list):
            out.extend(top)
        inner = obj.get("response")
        if isinstance(inner, dict) and isinstance(inner.get("candidates"), list):
            out.extend(inner["candidates"])
        return out

    # ── contract verdicts ────────────────────────────────────────────────
    def terminates_cleanly(self) -> bool:
        """True only when the stream ended on exactly one healthy finishReason
        candidate and was NOT preceded by a top-level error frame."""
        return (
            self.terminated
            and not self.saw_top_level_error
            and self.terminal_candidate_count == 1
        )


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 — the helper produces the sole terminal frame the parser ends on
# ──────────────────────────────────────────────────────────────────────────────

def test_terminal_frame_satisfies_parser_rule():
    frame = terminal_error_frame(502, "upstream blew up", "gemini-3-pro")
    st = _GeminiParserState()
    st.feed_bytes(gemini_sse_data(frame) + GEMINI_SSE_DONE)
    assert st.terminates_cleanly() is True
    assert st.done_count == 1


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 — the bare error frame (the old broken shape) does NOT terminate, and
# a subsequent finishReason frame does NOT rescue it once the parser errored.
# This documents WHY the bug existed and proves the model is not vacuous.
# ──────────────────────────────────────────────────────────────────────────────

def test_bare_error_then_stop_does_not_terminate():
    """THE DEFECT: error -> synthetic STOP -> [DONE] leaves the parser hung."""
    bare = {"error": {"code": 502, "message": "broken", "status": "UNAVAILABLE"}}
    st = _GeminiParserState()
    st.feed_bytes(
        gemini_sse_data(bare)
        + gemini_sse_data(terminal_error_frame(502, "broken", "gemini-3-pro"))
        + GEMINI_SSE_DONE
    )
    # The top-level error poisoned the parser; the later finishReason is not
    # acted on, so the stream never terminates from the parser's perspective.
    assert st.saw_top_level_error is True
    assert st.terminated is False
    assert st.terminates_cleanly() is False


def test_bare_error_frame_alone_does_not_terminate():
    bare = {"error": {"code": 502, "message": "broken", "status": "UNAVAILABLE"}}
    st = _GeminiParserState()
    st.feed_bytes(gemini_sse_data(bare) + GEMINI_SSE_DONE)
    assert st.terminates_cleanly() is False
    assert st.terminal_candidate_count == 0


# ──────────────────────────────────────────────────────────────────────────────
# Test 3 — STATIC INVARIANT. Scan app/main.py source and assert:
#   (a) every Gemini SSE error exit (content= with GEMINI_SSE_DONE, or a yield
#       of GEMINI_SSE_DONE inside the gemini egress) is paired with a
#       terminal_error_frame/_g_term call in the SAME block; and
#   (b) NO bare top-level `{"error": ...}` SSE data frame is yielded in the
#       gemini egress path (the poison that caused the freeze).
# Fails naming the offending line numbers. PASSES only after the 2026-08-07 fix.
# ──────────────────────────────────────────────────────────────────────────────

def _content_expressions_with_done(source: str):
    """Yield (start_lineno, span_text) for every `content=<expr>` whose
    expression body contains the token GEMINI_SSE_DONE (i.e. a Gemini SSE error
    exit — NOT the import line, NOT a `yield`).

    For each `content=` assignment we capture the text from `content=` up to
    the next sibling keyword argument (`status_code=`, `media_type=`, ...) or
    the closing `)` of the Response(...) call, so multi-line expressions are
    captured whole.
    """
    lines = source.splitlines()
    n = len(lines)
    for i, line in enumerate(lines):
        m = re.search(r"\bcontent\s*=\s*", line)
        if not m:
            continue
        block = _extract_content_block(lines, i, m.end())
        if block is None:
            continue
        if "GEMINI_SSE_DONE" in block:
            yield (i + 1, block)


def _extract_content_block(lines, start_idx, start_col):
    """Capture the text of the `content=<expr>` argument: from `content=` to
    the next sibling kwarg (`status_code=`, `media_type=`, `headers=`, ...) or
    the closing `)` of the surrounding call. Returns the joined text or None."""
    n = len(lines)
    collected = []
    first = True
    for r in range(start_idx, min(start_idx + 40, n)):
        ln = lines[r]
        seg = ln[start_col:] if first else ln
        first = False
        collected.append(seg)
        joined = "\n".join(collected)
        cut = re.search(
            r"\b(status_code|media_type|headers|background|response_id)\s*=",
            joined,
        )
        if cut:
            return joined[: cut.start()]
        if r > start_idx and re.search(r"^\s*\)", ln):
            return joined
    return "\n".join(collected)


def test_every_gemini_sse_error_exit_emits_terminal_frame():
    source = MAIN_PY.read_text(encoding="utf-8")
    offenders = []
    for (lineno, block) in _content_expressions_with_done(source):
        if "terminal_error_frame" in block or "_g_term" in block:
            continue
        offenders.append(lineno)
    if offenders:
        raise AssertionError(
            "Gemini SSE error exits missing a terminal_error_frame/_g_term call "
            "in their content= expression at lines: "
            + ", ".join(str(x) for x in offenders)
            + ". The Antigravity Gemini parser ignores bare error frames + "
            "[DONE], so these exits leave the IDE frozen."
        )


def test_no_bare_top_level_error_frame_in_gemini_egress():
    """The poison that caused the 2026-08-07 freeze: a yielded top-level
    `{"error": {...}}` (or an error-only payload variable) SSE data frame in
    the Gemini egress path. The terminal contract must be a single
    candidate-bearing frame (terminal_error_frame), never a preceding top-level
    error object.

    Scans every `yield <sse_alias>(...)` statement and flags it when its
    argument is a bare error source — a literal `{"error": ...}` dict or an
    error-payload variable (`error`, `error_payload`, `_err_payload`,
    `_exc_payload`, `_stall_err_obj`, `_drain_err_obj`, `err_obj`) — rather than
    the terminal_error_frame helper. Permits error text inside the helper's
    `parts` (candidate-wrapped, not top-level).
    """
    source = MAIN_PY.read_text(encoding="utf-8")
    lines = source.splitlines()
    offenders = []
    sse_alias = r"(?:_g_sse_data|gemini_sse_data)"
    # The argument right after the alias `(` — up to the first `,` or `)`.
    # We care about the FIRST token: is it a bare-error source?
    pat_yield = re.compile(rf"yield\s+{sse_alias}\(\s*(.+)$")
    bare_err_sources = {
        "error", "error_payload", "_err_payload", "_exc_payload",
        "_stall_err_obj", "_drain_err_obj", "err_obj",
    }
    for i, ln in enumerate(lines):
        m = pat_yield.search(ln)
        if not m:
            continue
        arg = m.group(1).strip()
        # First token of the argument expression.
        first_tok = arg.split("(", 1)[0].split(",", 1)[0].strip().rstrip("}")
        # Literal dict opening {"error" ...}
        if first_tok.startswith("{"):
            inner = first_tok.lstrip("{ ").strip().strip("'\"")
            if inner == "error":
                offenders.append(i + 1)
                continue
            # {"error": ...} inline on one line
            if "\"error\"" in first_tok or "'error'" in first_tok:
                offenders.append(i + 1)
                continue
        # Bare error-payload variable
        if first_tok in bare_err_sources:
            offenders.append(i + 1)
            continue
    if offenders:
        raise AssertionError(
            "Gemini egress yields a bare top-level {\"error\": ...} SSE data "
            "frame at lines: " + ", ".join(str(x) for x in offenders)
            + ". A top-level error object poisons the Antigravity Gemini parser "
            "(it stops consuming the later finishReason candidate), reproducing "
            "the 2026-08-07 IDE freeze. Emit terminal_error_frame ALONE instead."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Test 4 — real call into the credentials error path (non-200 terminal exit).
# ──────────────────────────────────────────────────────────────────────────────

def test_credentials_error_stream_terminates():
    import app.main as main

    resp = main._antigravity_native_credentials_error(
        source_model="some-unmapped-model",
        is_stream=True,
        fallback_reason="unmapped",
    )
    st = _GeminiParserState()
    st.feed_bytes(resp.body)
    assert st.terminates_cleanly() is True
    assert st.done_count == 1

    # The non-stream variant must still be a 401 JSONResponse.
    non_stream = main._antigravity_native_credentials_error(
        source_model="some-unmapped-model",
        is_stream=False,
        fallback_reason="unmapped",
    )
    from starlette.responses import JSONResponse
    assert isinstance(non_stream, JSONResponse)
    assert non_stream.status_code == 401
