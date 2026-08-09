"""
Stream-guard coverage tests.

THE DEFECT
Two StreamingResponse sites in app/main.py forwarded a RAW upstream body
(`upstream_response.aiter_raw()`) WITHOUT wrapping it in afz_guard:

  - the native-Google Gemini passthrough (_forward_antigravity_native)
  - the antigravity CCPA control-plane passthrough (_forward_antigravity_ccpa_control)

An unguarded StreamingResponse body is:
  - INVISIBLE to POST /api/antifreeze/force-stop (the user's emergency
    unfreeze button cannot cancel it),
  - subject to NO hard deadline (it can hang indefinitely),
  - able to leak its upstream httpx response on early cancellation.

Both sites are RAW PASSTHROUGHS: they forward upstream status, headers, and
content-type verbatim and may carry NON-SSE bodies. stream_deadline() always
injects an SSE terminal frame on timeout, which would CORRUPT a non-SSE
response, so the fix wraps each site through _afz_passthrough_guard(), which
selects a deadline+frame wrapper only for SSE content and a registry-only
wrapper otherwise.

These tests are static + unit — no server, no network, no mocking of main.py
beyond the guard primitives.
"""
import asyncio
import re
from pathlib import Path

from app.antifreeze import (
    ACTIVE_STREAMS,
    afz_guard,
    force_stop_all,
    next_stream_id,
)
import app.main as main


MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — STATIC SCAN: every StreamingResponse body must be guarded.
# ─────────────────────────────────────────────────────────────────────────────

# A StreamingResponse body is "guarded" if its first positional argument is:
#   (a) a direct afz_guard(...) / _afz_passthrough_guard(...) call, OR
#   (b) a Name that is assigned (directly or via tuple-unpack) from one of the
#       guard helpers within the preceding lines of the same function body.
#
# If a future site genuinely cannot be guarded, add it to ALLOWLIST with a
# written justification (line number + reason). An empty ALLOWLIST is the goal.
ALLOWLIST: dict[int, str] = {
    # <line_no>: "justification" — intentionally left empty: every site is
    # guarded. Add an entry ONLY with a concrete, written reason.
}


def _streaming_response_spans(source: str):
    """Yield (lineno, first_positional_arg_text) for every StreamingResponse(
    call in source.

    The first positional argument is the body generator. We capture from the
    token after `StreamingResponse(` up to the first top-level comma at paren
    depth 1 (i.e. the next positional/keyword argument) so multi-line and
    nested-call bodies are captured whole.
    """
    lines = source.splitlines()
    n = len(lines)
    for i, line in enumerate(lines):
        col = line.find("StreamingResponse(")
        if col == -1:
            continue
        # Start collecting right after the opening paren.
        depth = 0
        buf: list[str] = []
        start_col = col + len("StreamingResponse(")
        first = True
        for r in range(i, min(i + 30, n)):
            ln = lines[r]
            seg = ln[start_col:] if first else ln
            first = False
            for ch in seg:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        # End of the StreamingResponse(...) call.
                        yield (i + 1, "".join(buf).strip())
                        buf = None  # type: ignore[assignment]
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    yield (i + 1, "".join(buf).strip())
                    buf = None  # type: ignore[assignment]
                    break
                buf.append(ch)
            if buf is None:
                break
        else:
            # Ran off the end without closing — skip malformed.
            continue


_GUARD_TOKENS = ("afz_guard(", "_afz_passthrough_guard(")


def _name_of(arg_text: str):
    """If arg_text is a bare identifier, return it; else None."""
    t = arg_text.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
        return t
    return None


def _is_guarded_call(arg_text: str) -> bool:
    return any(tok in arg_text for tok in _GUARD_TOKENS)


def _assignment_is_guarded(source_lines: list[str], name: str, start_lineno: int) -> bool:
    """Look backwards from start_lineno for an assignment binding `name` to a
    guard helper call. Handles both `name = afz_guard(...)` and tuple-unpack
    `..., name = _afz_passthrough_guard(...)` / `name, _ = ...`."""
    pat_direct = re.compile(rf"\b{re.escape(name)}\s*=\s*([^\n=#]*?)$")
    pat_tuple = re.compile(rf"\b{re.escape(name)}\b")
    # Search the preceding 25 lines (same function body).
    lo = max(0, start_lineno - 1 - 25)
    for idx in range(start_lineno - 1, lo - 1, -1):
        ln = source_lines[idx]
        # Direct assignment: name = <expr>
        m = pat_direct.search(ln)
        if m and not ln.lstrip().startswith("#"):
            expr = m.group(1)
            if any(tok in expr for tok in _GUARD_TOKENS):
                return True
        # Tuple-unpack assignment containing name on the LHS.
        if "=" in ln and not ln.lstrip().startswith("#"):
            lhs = ln.split("=", 1)[0]
            if pat_tuple.search(lhs):
                rhs = ln.split("=", 1)[1]
                if any(tok in rhs for tok in _GUARD_TOKENS):
                    return True
    return False


def test_every_streaming_response_body_is_guarded():
    """Every StreamingResponse body in app/main.py must be wrapped by a guard.

    Fails naming the exact offending line number(s). A test that cannot fail is
    worthless — see the mutation check in this file's docstring procedure.
    """
    source = MAIN_PY.read_text(encoding="utf-8")
    lines = source.splitlines()
    offenders: list[int] = []
    for (lineno, arg_text) in _streaming_response_spans(source):
        if lineno in ALLOWLIST:
            continue
        if _is_guarded_call(arg_text):
            continue
        name = _name_of(arg_text)
        if name is not None and _assignment_is_guarded(lines, name, lineno):
            continue
        offenders.append(lineno)
    if offenders:
        raise AssertionError(
            "Unguarded StreamingResponse body at line(s): "
            + ", ".join(str(x) for x in offenders)
            + ". Every StreamingResponse body must be wrapped in afz_guard(...) "
            "or _afz_passthrough_guard(...) (directly or via assignment), or be "
            "listed in ALLOWLIST with a written justification. An unguarded body "
            "is invisible to POST /api/antifreeze/force-stop and has no deadline."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — a guarded stream registers in the kill-registry and unregisters.
# ─────────────────────────────────────────────────────────────────────────────

def test_guarded_stream_registers_in_kill_registry():
    """A guarded stream must appear in ACTIVE_STREAMS while running and be
    removed after completion, so force-stop can see it and the registry does
    not leak.

    Runs on its own event loop (asyncio.run), matching the repo's idiom in
    smoke_antifreeze.py — no pytest-asyncio plugin is installed."""

    async def scenario():
        async def body():
            yield b"data: hello\n\n"
            yield b"data: [DONE]\n\n"

        sid = next_stream_id()
        gen = afz_guard(body(), sid)
        ait = gen.__aiter__()
        # Drive one chunk so afz_guard's preamble (register_stream) has run.
        first = await ait.__anext__()
        assert first == b"data: hello\n\n"
        assert sid in ACTIVE_STREAMS, "guarded stream must be registered while running"

        # Drain to completion.
        rest = []
        async for chunk in ait:
            rest.append(chunk)
        assert b"[DONE]" in b"".join(rest)
        assert sid not in ACTIVE_STREAMS, "guarded stream must unregister after completion"

    asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — force_stop_all cancels a guarded (registered) stream.
# ─────────────────────────────────────────────────────────────────────────────

def test_force_stop_cancels_guarded_stream():
    """The user-visible behavior: the emergency unfreeze button must actually
    cancel a guarded stream that is hanging on a stuck upstream."""

    async def scenario():
        async def hanging_upstream():
            # Emulate a wedged leaf: never produces a chunk, never ends.
            await asyncio.sleep(3600)
            yield b""  # pragma: no cover

        sid = next_stream_id()
        gen = afz_guard(hanging_upstream(), sid)

        async def driver():
            async for _ in gen:
                pass  # pragma: no cover - should be cancelled before yielding

        task = asyncio.ensure_future(driver())

        # Let the driver enter afz_guard so register_stream has run.
        for _ in range(200):
            await asyncio.sleep(0)
            if sid in ACTIVE_STREAMS:
                break
        assert sid in ACTIVE_STREAMS, "stream should be registered before force-stop"

        cancelled = await force_stop_all()
        assert cancelled >= 1, "force_stop_all must cancel the registered stream"

        # The driver task must actually be cancelled by the registry cancel.
        try:
            await task
            raised = False
        except asyncio.CancelledError:
            raised = True
        assert raised, "driver task must be cancelled by force_stop_all"
        assert sid not in ACTIVE_STREAMS

    asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — a non-SSE passthrough is NOT corrupted by injected SSE frames.
# ─────────────────────────────────────────────────────────────────────────────

def test_non_sse_passthrough_not_corrupted():
    """Constraint (b): a non-SSE passthrough response must NOT gain injected
    SSE frames. _afz_passthrough_guard must choose the registry-only wrapper
    (no deadline, no frames) for non-event-stream content-types, and the
    deadline wrapper only for text/event-stream.

    Proven three ways:
      1. Non-SSE JSON control-plane body, allow_deadline_frames=False:
         registry-only wrapper, bytes untouched, no sentinel.
      2. Non-SSE body, allow_deadline_frames=True: STILL registry-only, because
         content-type gating decides — so a stuck non-SSE stream is
         force-stoppable but never frame-corrupted.
      3. SSE content-type: the full afz_guard deadline wrapper is chosen.
    """

    async def scenario():
        # (1) Non-SSE JSON control-plane body: registry-only, bytes untouched.
        json_payload = b'{"error": {"code": 200, "rpc": "ListModels"}}'

        async def json_body():
            yield json_payload

        sid, body = main._afz_passthrough_guard(
            json_body(), "application/json; charset=utf-8",
            allow_deadline_frames=False,
        )
        assert body.__name__ == "_afz_registry_only_guard", (
            "non-SSE passthrough must use the registry-only wrapper, got "
            f"{body.__name__!r}"
        )
        out = []
        async for chunk in body:
            out.append(chunk)
        joined = b"".join(out)
        assert joined == json_payload, "non-SSE bytes must pass through verbatim"
        assert b"[DONE]" not in joined, "no SSE sentinel must be injected"
        assert b"text/event-stream" not in joined
        assert sid not in ACTIVE_STREAMS, "registry-only wrapper must unregister"

        # (2) Non-SSE content-type with allow_deadline_frames=True still picks
        #     registry-only (content-type gating), so it is force-stoppable but
        #     never frame-corrupted.
        async def slow_non_sse():
            await asyncio.sleep(3600)
            yield b"never"  # pragma: no cover

        sid2, body2 = main._afz_passthrough_guard(
            slow_non_sse(), "application/json", allow_deadline_frames=True,
        )
        assert body2.__name__ == "_afz_registry_only_guard", (
            "non-SSE content-type must skip the deadline wrapper even when "
            "allow_deadline_frames=True"
        )
        # Clean up the registered-but-not-driven generator.
        await body2.aclose()

        # (3) SSE content-type with allow_deadline_frames=True picks afz_guard.
        async def sse_body():
            yield b"data: hi\n\n"

        sid3, body3 = main._afz_passthrough_guard(
            sse_body(), "text/event-stream", protocol="gemini",
        )
        assert body3.__name__ == "afz_guard", (
            "SSE passthrough must use the full afz_guard deadline wrapper"
        )
        out3 = []
        async for chunk in body3:
            out3.append(chunk)
        assert b"".join(out3) == b"data: hi\n\n"
        assert sid3 not in ACTIVE_STREAMS

    asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────────
# Mutation-check scaffolding (documentation of the manual procedure).
#
# The task requires a manual mutation check: temporarily unwrap ONE of the two
# newly-guarded sites, confirm test_every_streaming_response_body_is_guarded
# FAILS and names that line, then restore it and confirm it PASSES. That
# procedure is performed out-of-band and reported in the task summary; it
# cannot be encoded as a self-contained pytest case without mutating source on
# disk, which would race with the other tests in the suite.
#
# The non-vacuity of the scan is instead guaranteed structurally:
#  - _streaming_response_spans finds the call by text, not by AST of a guard.
#  - a bare `upstream_response.aiter_raw()` body is neither a guard call nor a
#    Name with a guarded assignment, so it is reported as an offender.
# This is demonstrated by test_scan_flags_a_bare_aiter_raw_body below.
# ─────────────────────────────────────────────────────────────────────────────

def test_scan_flags_a_bare_aiter_raw_body():
    """Non-vacuity proof: the scan logic flags the exact pre-fix shape
    (`StreamingResponse(upstream_response.aiter_raw(), ...)`) as unguarded."""
    sample = (
        "def handler():\n"
        "    return StreamingResponse(\n"
        "        upstream_response.aiter_raw(),\n"
        "        status_code=200,\n"
        "    )\n"
    )
    spans = list(_streaming_response_spans(sample))
    assert spans, "scan must locate the StreamingResponse call"
    (lineno, arg_text) = spans[0]
    assert "aiter_raw" in arg_text
    assert not _is_guarded_call(arg_text)
    # And it is not rescuable as a guarded Name assignment.
    name = _name_of(arg_text)
    assert name is None or not _assignment_is_guarded(
        sample.splitlines(), name, lineno
    )
