"""2026-10-02 idle-parity mirror regression for the Anthropic→OpenAI egress.

The lane serving OpenAI-format clients (app/main.py, the
`anthropic_to_openai_egress_stream` function containing `_raw_anthropic_ok()`,
pump label "anthropic-to-openai") iterated
`_transport_guarded(_pump(resp.aiter_raw(), ...), stats, _emit)` DIRECTLY. A
wedged upstream (content streams, then the relay holds the TCP socket open
with no [DONE]/EOF — the observed MintRouter failure) blocked that `async for`
FOREVER: no cap, no keepalive tick, no client-disconnect check. Same freeze
class as the Gemini lane's BUG A (2026-09-27).

THE MIRROR (2026-10-02): wrap the guarded pump in a drain task feeding an
asyncio.Queue; replace the direct `async for` with a
`while True` + `asyncio.wait_for(_q.get(), GEMINI_EGRESS_KEEPALIVE_INTERVAL)`
loop; on each TimeoutError tick check client disconnect (499), then the
GEMINI_EGRESS_MAX_IDLE cap (stats["error"]="upstream_idle_timeout" +
stats["status"]=504 + break), else yield `b": keepalive\\n\\n"` (a valid SSE
comment for OpenAI clients). The idle timer resets ONLY on a real chunk —
never on a keepalive tick. A drain error is re-raised after cleanup so the
egress's existing combo/transport catchers run unchanged. The lane's
post-drain stall tuple now carries "upstream_idle_timeout" so the exit is the
OpenAI terminal error frames + [DONE] (or combo-fallback pre-content) —
NEVER a bare [DONE] with status 200.

Pure-logic, no server, no network — the transport loop is exercised via an
inline reconstruction of the exact production control flow (same
wait_for/_SENTINEL/keepalive/idle-cap shape as the Gemini idle tests), with
the REAL production helpers (StreamEmissionState,
_openai_terminal_error_frames) driving the post-drain contract.
"""

import asyncio
import inspect
import time

import app.main as main
from app.middleware.stream_guard import StreamEmissionState


def _make_a2o_loop(max_idle: float, keepalive_interval: float = 0.02):
    """Build the anthropic-to-openai transport loop's control flow as a
    standalone async generator that mirrors the production shape in
    app/main.py::_raw_anthropic_ok (2026-10-02 mirror):

      - the guarded pump runs in a drain task feeding an asyncio.Queue;
        completion/error enqueues the terminal _SENTINEL and captures the
        error for re-raise (production: _drain_a2o_source / _a2o_drain_err).
      - the loop `wait_for(_q.get(), keepalive_interval)`; on TimeoutError it
        checks client disconnect (499), then the idle cap (504), then yields
        a keepalive comment.
      - the idle timer resets ONLY on a real chunk, never on a keepalive tick.
      - on exit the drain task is cancelled/awaited (production finally).

    `upstream_chunks` is the sequence the drain task enqueues; when it is
    exhausted the drain task enqueues _SENTINEL (the normal end). A wedged
    relay is simulated by an iterator that NEVER exhausts — only the idle cap
    can end the loop. Returns (drain, SENTINEL) where
    drain(upstream_chunks, client_disconnected=..., drain_error=None)
    -> (emitted, stats, raised).
    """
    SENTINEL = object()

    async def drain(upstream_chunks, client_disconnected=lambda: False, drain_error=None):
        q: asyncio.Queue = asyncio.Queue()
        captured_err = None
        stats = {"status": 200, "error": None, "out": 0, "ttft": 0.0}

        async def drain_a2o_source():
            nonlocal captured_err
            try:
                async for _c in upstream_chunks():
                    await q.put(_c)
                if drain_error is not None:
                    # Simulate the pump raising AFTER its chunks were drained
                    # (e.g. midstream transport death at the iterator boundary).
                    raise drain_error
            except Exception as _de:  # mirror production: capture, don't crash
                captured_err = _de
            finally:
                await q.put(SENTINEL)

        drain_task = asyncio.ensure_future(drain_a2o_source())
        last_upstream_data = time.monotonic()
        emitted = []
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=keepalive_interval)
                except asyncio.TimeoutError:
                    if client_disconnected():
                        stats["error"] = stats["error"] or "client_disconnected"
                        stats["status"] = 499
                        break
                    idle = time.monotonic() - last_upstream_data
                    if idle > max_idle:
                        emitted.append(("IDLE_TIMEOUT", idle))
                        stats["error"] = stats.get("error") or "upstream_idle_timeout"
                        stats["status"] = 504
                        break
                    emitted.append(b": keepalive\n\n")
                    continue
                if chunk is SENTINEL:
                    break
                last_upstream_data = time.monotonic()  # real data resets the timer
                if b'"text_delta"' in chunk:
                    stats["ttft"] = time.time()
                    stats["out"] = max(stats["out"], 1)
                emitted.append(chunk)
        finally:
            if not drain_task.done():
                drain_task.cancel()
            try:
                await drain_task
            except BaseException:
                pass
        # Production 2026-10-02: a captured drain error re-raises out of the
        # generator so the egress's existing catchers run unchanged.
        raised = None
        if captured_err is not None:
            raised = captured_err
        return emitted, stats, raised

    return drain, SENTINEL


def _wedged_anthropic_frames():
    """Anthropic-SSE-shaped content frames (the lane's native upstream format)
    followed by eternal silence."""
    first = (
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    )
    return first


# ─── the mirror: the transport loop must end a wedged upstream ─────────────


def test_a2o_loop_breaks_on_idle_cap_when_upstream_never_closes():
    """The exact MintRouter freeze class on this lane: upstream streams
    content then holds the socket open forever. Without the mirror the
    `async for` would block indefinitely. With it, the loop MUST break once
    `max_idle` elapses with no real chunk, carrying the 504 + error routing
    that the post-drain branch reads."""
    drain, _SENTINEL = _make_a2o_loop(max_idle=0.05, keepalive_interval=0.01)

    async def wedged_upstream():
        yield _wedged_anthropic_frames()
        # Block forever — no [DONE], no EOF, no exception.
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream)

    emitted, stats, raised = asyncio.run(run())

    # Real content was forwarded before the freeze.
    assert any(b'"text_delta"' in c for c in emitted if isinstance(c, (bytes, bytearray)))
    # The idle cap fired and ended the loop (not the _SENTINEL, which never
    # arrived because the upstream never closed).
    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert len(idle_events) == 1, f"expected exactly one idle-timeout break, got {idle_events}"
    assert idle_events[0][1] >= 0.05
    # The break carries the routing the post-drain branch needs.
    assert stats["error"] == "upstream_idle_timeout"
    assert stats["status"] == 504
    assert raised is None


def test_a2o_keepalive_ticks_below_cap_yield_sse_comments():
    """Keepalive comments must still be yielded on sub-cap ticks, and a stream
    whose chunks keep arriving (gaps < cap) never trips the cap — healthy
    long-thinking streams are unaffected."""
    drain, _SENTINEL = _make_a2o_loop(max_idle=0.2, keepalive_interval=0.01)

    async def slow_thinker():
        # Chunks every ~0.05s — each is real data that resets the timer.
        for _ in range(4):
            yield b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"tok"}}\n\n'
            await asyncio.sleep(0.05)

    async def run():
        return await drain(slow_thinker)

    emitted, stats, raised = asyncio.run(run())
    keepalives = [c for c in emitted if isinstance(c, bytes) and c == b": keepalive\n\n"]
    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert keepalives, "sub-cap ticks must still yield : keepalive comments"
    assert idle_events == [], f"healthy chunk cadence must never trip the cap: {idle_events}"
    assert sum(1 for c in emitted if isinstance(c, (bytes, bytearray)) and b'"text_delta"' in c) == 4
    assert stats["error"] is None
    assert stats["status"] == 200
    assert raised is None


def test_a2o_client_disconnect_during_idle_breaks_499():
    """Client-disconnect during idle must yield 499 semantics: the check runs
    FIRST on every keepalive tick, so a disconnected client ends the loop
    before the idle cap is consulted."""
    drain, _SENTINEL = _make_a2o_loop(max_idle=10.0, keepalive_interval=0.01)

    async def wedged_upstream():
        yield _wedged_anthropic_frames()
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream, client_disconnected=lambda: True)

    emitted, stats, raised = asyncio.run(run())
    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert idle_events == [], "client disconnect must pre-empt the idle cap"
    assert stats["error"] == "client_disconnected"
    assert stats["status"] == 499


def test_a2o_drain_error_re_raised_after_loop():
    """A pump exception captured by the drain task (combo fallback signal,
    deadline retry, transport death) must re-raise out of the generator after
    cleanup — exactly as it propagated when the pump was iterated directly —
    so the egress's existing catchers run unchanged."""
    class _FakeComboFallback(Exception):
        pass

    drain, _SENTINEL = _make_a2o_loop(max_idle=10.0, keepalive_interval=0.01)

    async def dying_upstream():
        yield _wedged_anthropic_frames()
        # Drain completes normally, then the pump "raises" (simulated by the
        # harness's drain_error injection at the iterator boundary).

    async def run():
        return await drain(dying_upstream, drain_error=_FakeComboFallback("advance"))

    emitted, stats, raised = asyncio.run(run())
    assert isinstance(raised, _FakeComboFallback)
    assert any(b'"text_delta"' in c for c in emitted if isinstance(c, (bytes, bytearray)))
    assert stats["status"] == 200  # the loop itself ended normally (SENTINEL)


# ─── the exit: never a bare [DONE] with status 200 ──────────────────────────


def test_a2o_post_content_idle_emits_openai_terminal_frames():
    """POST-CONTENT idle expiry (content streamed, then silence) must end with
    the OpenAI error SSE frame + `data: [DONE]` (the REAL production builder
    _openai_terminal_error_frames), status 504, error
    "upstream_idle_timeout" — never a bare [DONE] with status 200."""
    drain, _SENTINEL = _make_a2o_loop(max_idle=0.05, keepalive_interval=0.01)

    async def wedged_upstream():
        yield _wedged_anthropic_frames()
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream)

    emitted, stats, raised = asyncio.run(run())
    assert stats["error"] == "upstream_idle_timeout"
    assert stats["status"] == 504
    assert raised is None

    # Model the post-drain branch with the REAL emission guard: content was
    # marked emitted, so the precheck must REFUSE fallback — the only legal
    # exit is the OpenAI terminal sequence.
    _emit = StreamEmissionState()
    _emit.mark_emitted(_wedged_anthropic_frames())
    assert _emit.may_fallback("stream_stall_precheck") is False, (
        "post-content fallback must be refused — a splice would corrupt the live parser"
    )
    tail = main._openai_terminal_error_frames("upstream_idle_timeout", "test-model", 504)
    assert len(tail) >= 3
    # Error frame: OpenAI-shaped {"error": {...}} with the 504 code.
    assert b'"error"' in tail[0]
    assert b"upstream_idle_timeout" in tail[0]
    assert b"504" in tail[0]
    # Final frame is [DONE] — the OpenAI client unblocks on it.
    assert tail[-1] == b"data: [DONE]\n\n"
    # The invariant: the client NEVER receives a bare [DONE] only (the error
    # frame precedes it), and status is never left at 200.
    joined = b"".join(c for c in emitted if isinstance(c, (bytes, bytearray))) + b"".join(tail)
    assert b"[DONE]" in joined
    assert b'"error"' in joined


def test_a2o_pre_content_idle_never_bare_done_200():
    """PRE-CONTENT idle expiry (no bytes ever): combo fallback advances to the
    next chain entry (modeled here — may_fallback must ALLOW it) or, with the
    chain exhausted, emits the terminal frames. The invariant: NEVER a bare
    [DONE] with status 200."""
    drain, _SENTINEL = _make_a2o_loop(max_idle=0.03, keepalive_interval=0.01)

    async def wedged_from_start():
        # No bytes ever — the relay accepts the connection and goes silent.
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - unreachable; makes this an async generator

    async def run():
        return await drain(wedged_from_start)

    emitted, stats, raised = asyncio.run(run())
    assert stats["error"] == "upstream_idle_timeout"
    assert stats["status"] == 504
    # Nothing reached the client pre-content.
    data_frames = [c for c in emitted if isinstance(c, (bytes, bytearray)) and c.startswith(b"data:")]
    assert data_frames == []

    _emit = StreamEmissionState()
    assert _emit.may_fallback("stream_stall_precheck") is True, (
        "pre-content idle MUST be fallback-eligible — combo advance, not a bare [DONE]"
    )
    # Chain-exhausted exit (the production fallback-refused branch): the REAL
    # OpenAI terminal builder, ending in [DONE] — never bare.
    tail = main._openai_terminal_error_frames("upstream_idle_timeout", "test-model", 504)
    assert b'"error"' in tail[0]
    assert tail[-1] == b"data: [DONE]\n\n"


# ─── production source pins (the mirror is IN app/main.py) ──────────────────


def _pass_source() -> str:
    """Source of _process_chat_completion_pass — the anthropic-to-openai
    egress (and its `_raw_anthropic_ok`) is nested inside it. Pins by TEXT,
    never by line number."""
    return inspect.getsource(main._process_chat_completion_pass)


def test_production_a2o_loop_is_idle_capped():
    """PIN: the anthropic-to-openai lane must iterate the guarded pump through
    the queue-drain + wait_for loop (not directly), gated on
    GEMINI_EGRESS_MAX_IDLE with the 504 status assignment at the break."""
    src = _pass_source()
    # The direct iteration shape must be GONE (pre-fix: `async for chunk in
    # _transport_guarded(_pump(resp.aiter_raw(), _emit, _attempt,
    # "anthropic-to-openai"), stats, _emit):`).
    assert 'async for chunk in _transport_guarded(_pump(resp.aiter_raw(), _emit, _attempt, "anthropic-to-openai")' not in src, (
        "the anthropic-to-openai lane must not iterate the guarded pump directly — "
        "a wedged upstream blocks it forever (2026-10-02 mirror)"
    )
    # The pump is still labeled anthropic-to-openai and runs inside a drain task.
    assert '"anthropic-to-openai"' in src
    assert "asyncio.wait_for(" in src
    assert "_drain_a2o_source" in src
    # The idle break sets the 504 routing.
    anchor = 'stats["error"] = stats.get("error") or "upstream_idle_timeout"'
    idx = src.find(anchor)
    assert idx != -1, "the a2o idle site must exist"
    window = src[idx: idx + 400]
    assert 'stats["status"] = 504' in window, "the a2o idle break must set status 504"
    assert "break" in window


def test_production_a2o_stall_tuple_includes_idle_error():
    """PIN: the lane's post-drain stall tuple must carry
    "upstream_idle_timeout" so an idle break emits the OpenAI terminal frames
    instead of a bare [DONE]."""
    src = _pass_source()
    legacy = src.count('in ("ttft_stall", "stream_stall"):')
    assert legacy == 0, (
        f"{legacy} stall tuple(s) still lack upstream_idle_timeout — an idle "
        "break there would end in a bare [DONE] with status 200"
    )
    assert src.count('in ("ttft_stall", "stream_stall", "upstream_idle_timeout"):') >= 4


def test_a2o_module_constants_unchanged():
    """The mirror reuses the lane-generic ceilings verbatim — no rename, no
    value change (constraint from the 2026-10-02 fix)."""
    assert main.GEMINI_EGRESS_MAX_IDLE == 300.0
    assert main.GEMINI_EGRESS_KEEPALIVE_INTERVAL == 2.0
