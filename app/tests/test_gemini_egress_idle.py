"""BUG A / BUG B (2026-09-27) regression for the Gemini egress path.

Two coupled MintRouter defects on the Antigravity IDE (client=gemini) path in
app/main.py::gemini_egress_stream, which is NOT the OpenAI _pump path:

BUG A — STREAM FREEZE. MintRouter streams content then holds the TCP socket
open without [DONE]/no EOF. The drain loop's keepalive (`: keepalive`) yielded
forever: upstream never closed -> `_drain_openai_source` never finished ->
the queue never saw the `_SENTINEL` -> the response never completed -> the
IDE's per-conversation lock stayed held until the user killed every other
IDE instance.

    Fix: `GEMINI_EGRESS_MAX_IDLE` (300s) caps a silent upstream. The drain
    loop tracks `_last_upstream_data` (monotonic), reset ONLY on a real chunk
    (never on a keepalive tick), and breaks when no bytes arrive for the full
    window — so existing terminal/zero-token/combo-fallback handling runs and
    the stream closes cleanly. 300s sits above the worst observed reasoning
    gap (~180s), so a healthy long-thinking stream (every chunk resets) never
    trips it.

BUG B — PERSISTENT In:0/Out:0. MintRouter omits the terminal usage frame, so
the inline usage reader in `_raw_ok` never populated stats, the zero-output
guard mis-fired as a dead leaf, and a working provider was benched/blocked.
The OpenAI _pump path already wired `_backfill_missing_usage` (~L9668); the
Gemini path did not.

    Fix: call `_backfill_missing_usage` once, after the drain loop completes
    and before the zero-token 200 guard, mirroring the OpenAI site. The helper
    returns early if a real usage frame populated stats and never masks a
    genuine dead leaf (empty partial_text).

Pure-logic, no server, no network. The drain loop is exercised via an inline
reconstruction of the exact production control flow (the same `wait_for`/
`_SENTINEL`/keepalive/idle-cap shape), not a FastAPI boot — the loop's idle
contract is independent of the enclosing ASGI plumbing.
"""

import asyncio
import time

import app.main as main


# ─── BUG A: the drain loop must end a wedged (never-closing) upstream ──────


def _make_drain_loop(max_idle: float, keepalive_interval: float = 0.02):
    """Build the Gemini egress drain loop's control flow as a standalone async
    generator that mirrors the production shape in app/main.py:

      - `_drain_openai_source` puts real chunks + a terminal `_SENTINEL`.
      - the loop `wait_for(_q.get(), keepalive_interval)`; on TimeoutError it
        checks client disconnect, then the idle cap, then yields a keepalive.
      - the idle timer resets ONLY on a real chunk, never on a keepalive tick.

    `upstream_chunks` is the sequence the drain task enqueues; when it is
    exhausted the drain task enqueues `_SENTINEL` (the normal end). A wedged
    relay is simulated by an `upstream_chunks` iterator that NEVER exhausts
    (so no `_SENTINEL` is ever enqueued) — only the idle cap can end the loop.
    """
    SENTINEL = object()

    async def drain(upstream_chunks, client_disconnected=lambda: False):
        q: asyncio.Queue = asyncio.Queue()
        drain_err = None

        async def drain_openai_source():
            nonlocal drain_err
            try:
                async for _c in upstream_chunks():
                    await q.put(_c)
            except Exception as _de:  # mirror production: capture, don't crash
                drain_err = _de
            finally:
                await q.put(SENTINEL)

        drain_task = asyncio.ensure_future(drain_openai_source())
        last_upstream_data = time.monotonic()
        emitted = []
        try:
            while True:
                try:
                    ochunk = await asyncio.wait_for(q.get(), timeout=keepalive_interval)
                except asyncio.TimeoutError:
                    if client_disconnected():
                        break
                    idle = time.monotonic() - last_upstream_data
                    if idle > max_idle:
                        emitted.append(("IDLE_TIMEOUT", idle))
                        break
                    emitted.append(b": keepalive\n\n")
                    continue
                if ochunk is SENTINEL:
                    break
                last_upstream_data = time.monotonic()  # real data resets the timer
                emitted.append(ochunk)
        finally:
            if not drain_task.done():
                drain_task.cancel()
            try:
                await drain_task
            except BaseException:
                pass
        return emitted

    return drain, SENTINEL


def test_drain_loop_breaks_on_idle_cap_when_upstream_never_closes():
    """The exact MintRouter freeze: upstream streams content then holds the
    socket open forever (no _SENTINEL ever enqueued). Without the idle cap the
    loop would yield keepalives indefinitely. With it, the loop MUST break
    once `max_idle` elapses with no real chunk, so the response completes."""
    drain, _SENTINEL = _make_drain_loop(max_idle=0.05, keepalive_interval=0.01)

    async def wedged_upstream():
        # One real chunk, then the relay goes silent forever (never returns,
        # never raises — the socket stays half-open). Production MintRouter.
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        # Block forever — no [DONE], no EOF, no exception. The drain task
        # never reaches its `finally: q.put(SENTINEL)`.
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream)

    emitted = asyncio.run(run())

    # Real content was forwarded before the freeze.
    assert any(b'"content":"hi"' in c for c in emitted if isinstance(c, (bytes, bytearray)))
    # The idle cap fired and ended the loop (not the _SENTINEL, which never
    # arrived because the upstream never closed).
    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert len(idle_events) == 1, f"expected exactly one idle-timeout break, got {idle_events}"
    assert idle_events[0][1] >= 0.05


def test_drain_loop_completes_normally_when_upstream_closes():
    """Regression guard: the idle cap must NOT end a healthy stream early. When
    upstream closes cleanly (drain task enqueues _SENTINEL), the loop ends via
    the sentinel and the idle cap is never consulted."""
    drain, _SENTINEL = _make_drain_loop(max_idle=0.05, keepalive_interval=0.01)

    async def closing_upstream():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
        # Normal end: iterator exhausts -> drain task enqueues _SENTINEL.

    async def run():
        return await drain(closing_upstream)

    emitted = asyncio.run(run())

    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert idle_events == [], f"healthy close must not trip idle cap: {idle_events}"
    assert any(b'"content":"hi"' in c for c in emitted if isinstance(c, (bytes, bytearray)))


def test_keepalive_ticks_do_not_reset_idle_timer():
    """The half of BUG A that makes the cap work: a keepalive tick is NOT
    upstream data. If the timer reset on keepalives, a wedged relay would run
    forever (every keepalive would re-arm the window). Only a real chunk may
    reset it. Construct a stream that yields one chunk, then after the first
    keepalive yields another chunk JUST under the cap, then goes silent; the
    cap must still fire from the SECOND chunk's timestamp, not the keepalive."""
    drain, _SENTINEL = _make_drain_loop(max_idle=0.06, keepalive_interval=0.01)

    async def burst_then_wedge():
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        # Let a couple of keepalive ticks happen (no reset), then yield a
        # second real chunk — this resets the timer. Then wedge forever.
        await asyncio.sleep(0.03)
        yield b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        await asyncio.Event().wait()

    async def run():
        return await drain(burst_then_wedge)

    emitted = asyncio.run(run())

    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert len(idle_events) == 1, f"expected one idle break after the second chunk, got {idle_events}"
    # Both real chunks forwarded.
    assert any(b'"content":"a"' in c for c in emitted if isinstance(c, (bytes, bytearray)))
    assert any(b'"content":"b"' in c for c in emitted if isinstance(c, (bytes, bytearray)))


def test_client_disconnect_breaks_before_idle_cap():
    """The client-abort check stays first: a disconnected client ends the loop
    immediately, the idle cap is never consulted."""
    drain, _SENTINEL = _make_drain_loop(max_idle=10.0, keepalive_interval=0.01)

    async def wedged_upstream():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream, client_disconnected=lambda: True)

    emitted = asyncio.run(run())

    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert idle_events == [], "client disconnect must pre-empt the idle cap"


def test_module_constant_present_and_above_reasoning_gap_ceiling():
    """The cap must sit above the worst observed legit reasoning gap (~180s,
    see GEMINI_EGRESS_BODY_STALL_TIMEOUT rationale) so healthy thinking
    streams never trip it. 300s is the documented fail-safe ceiling."""
    assert hasattr(main, "GEMINI_EGRESS_MAX_IDLE")
    assert main.GEMINI_EGRESS_MAX_IDLE == 300.0
    assert main.GEMINI_EGRESS_MAX_IDLE > 180.0
    # The keepalive interval is unchanged (constraint: do not regress).
    assert main.GEMINI_EGRESS_KEEPALIVE_INTERVAL == 2.0


# ─── 2026-10-02 TERMINAL-FRAME FIX: idle expiry must set status + route ─────
#
# BUG A's break set NO HTTP status and NO terminal error frame: after the loop
# the post-drain error branch only matched ("ttft_stall", "stream_stall"), so
# "upstream_idle_timeout" fell through to a bare `data: [DONE]` with status
# 200. The IDE parser requires a terminal error frame for any non-success
# termination — it hangs waiting for one. That is the reported freeze.


def _make_drain_loop_with_stats(max_idle: float, keepalive_interval: float = 0.02):
    """Mirror of the production drain loop INCLUDING the 2026-10-02 fix: the
    idle-expiry site sets stats["status"] = 504 alongside the error before the
    break, exactly as app/main.py now does. Returns (drain, SENTINEL) where
    drain(upstream_chunks, client_disconnected=...) -> (emitted, stats)."""
    SENTINEL = object()

    async def drain(upstream_chunks, client_disconnected=lambda: False):
        q: asyncio.Queue = asyncio.Queue()
        drain_err = None
        stats = {"status": 200, "error": None}

        async def drain_openai_source():
            nonlocal drain_err
            try:
                async for _c in upstream_chunks():
                    await q.put(_c)
            except Exception as _de:  # mirror production: capture, don't crash
                drain_err = _de
            finally:
                await q.put(SENTINEL)

        drain_task = asyncio.ensure_future(drain_openai_source())
        last_upstream_data = time.monotonic()
        emitted = []
        try:
            while True:
                try:
                    ochunk = await asyncio.wait_for(q.get(), timeout=keepalive_interval)
                except asyncio.TimeoutError:
                    if client_disconnected():
                        stats["error"] = stats["error"] or "client_disconnected"
                        stats["status"] = 499
                        break
                    idle = time.monotonic() - last_upstream_data
                    if idle > max_idle:
                        emitted.append(("IDLE_TIMEOUT", idle))
                        stats["error"] = stats.get("error") or "upstream_idle_timeout"
                        # 2026-10-02 fix: status is set at the break site so
                        # the post-drain error branch actually fires.
                        stats["status"] = 504
                        break
                    emitted.append(b": keepalive\n\n")
                    continue
                if ochunk is SENTINEL:
                    break
                last_upstream_data = time.monotonic()  # real data resets the timer
                emitted.append(ochunk)
        finally:
            if not drain_task.done():
                drain_task.cancel()
            try:
                await drain_task
            except BaseException:
                pass
        return emitted, stats

    return drain, SENTINEL


def test_idle_expiry_sets_status_504_and_error_in_loop():
    """The 2026-10-02 fix: when the idle cap breaks the loop, stats must carry
    status=504 + error="upstream_idle_timeout" AT THE BREAK SITE — that is what
    routes the post-drain branch off the bare-[DONE] fallthrough."""
    drain, _SENTINEL = _make_drain_loop_with_stats(max_idle=0.05, keepalive_interval=0.01)

    async def wedged_upstream():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream)

    emitted, stats = asyncio.run(run())
    assert stats["error"] == "upstream_idle_timeout"
    assert stats["status"] == 504
    assert any(b'"content":"hi"' in c for c in emitted if isinstance(c, (bytes, bytearray)))


def test_post_content_idle_emits_gemini_terminal_frame_then_done():
    """POST-CONTENT idle expiry (content streamed, then silence) must terminate
    with the Gemini terminal error frame (504, "upstream_idle_timeout")
    followed by `data: [DONE]` — and NOTHING else after the content. No
    combo-fallback splice can happen post-content (_emit.may_fallback refuses),
    and no bare {"error":...} frame may precede the terminal frame (it poisons
    the Gemini parser — FREEZE FIX 2026-08-07 contract)."""
    from app.compat.adapters.gemini import SSE_DONE, sse_data, terminal_error_frame
    from app.middleware.stream_guard import StreamEmissionState

    drain, _SENTINEL = _make_drain_loop_with_stats(max_idle=0.05, keepalive_interval=0.01)

    async def wedged_upstream():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        await asyncio.Event().wait()

    async def run():
        return await drain(wedged_upstream)

    emitted, stats = asyncio.run(run())

    # Model the post-drain stall branch with the REAL production helpers: the
    # content yield marked emission, so may_fallback must refuse and the SOLE
    # terminal contract (terminal frame + [DONE]) is the only legal exit.
    _emit = StreamEmissionState()
    _emit.mark_emitted(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    assert _emit.may_fallback("upstream_idle_timeout") is False, (
        "post-content fallback must be refused — a splice here would wedge the parser"
    )
    tail = [sse_data(terminal_error_frame(504, "upstream_idle_timeout", "test-model")), SSE_DONE]

    # stats carry the 504 routing the branch reads.
    assert stats["status"] == 504
    assert stats["error"] == "upstream_idle_timeout"
    # Terminal frame shape: response envelope with finishReason, visible error text.
    frame_obj = tail[0]
    assert b"finishReason" in frame_obj
    assert b'"response"' in frame_obj
    assert b"upstream_idle_timeout" in frame_obj
    assert b"[DONE]" in tail[1]
    # NO bare top-level {"error":...} frame before the terminal frame.
    joined = b"".join(c for c in emitted if isinstance(c, (bytes, bytearray))) + b"".join(tail)
    first_error_idx = joined.find(b'{"error"')
    term_idx = joined.find(b"finishReason")
    assert first_error_idx == -1 or first_error_idx > term_idx, (
        "a bare {\"error\":...} frame must never precede the terminal frame"
    )


def test_pre_content_idle_never_bare_done_200():
    """PRE-CONTENT idle expiry (no bytes ever): combo fallback advances to the
    next chain entry (modeled here) or wraps / emits the terminal frame when
    the chain is exhausted. The invariant: NEVER a bare [DONE] with status 200
    — the branch fires because the tuple now carries upstream_idle_timeout."""
    from app.compat.adapters.gemini import SSE_DONE, sse_data, terminal_error_frame
    from app.middleware.stream_guard import StreamEmissionState

    drain, _SENTINEL = _make_drain_loop_with_stats(max_idle=0.03, keepalive_interval=0.01)

    async def wedged_from_start():
        # No bytes ever — the relay accepts the connection and goes silent.
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - unreachable; makes this an async generator

    async def run():
        return await drain(wedged_from_start)

    emitted, stats = asyncio.run(run())
    assert stats["error"] == "upstream_idle_timeout"
    assert stats["status"] == 504
    # Nothing was forwarded to the client pre-content.
    data_frames = [c for c in emitted if isinstance(c, (bytes, bytearray)) and c.startswith(b"data:")]
    assert data_frames == []

    # Model the post-drain branch exactly as production reads it: the tuple
    # match routes a pre-content idle through the combo-advance rail first.
    _emit = StreamEmissionState()
    assert _emit.may_fallback("upstream_idle_timeout") is True, (
        "pre-content idle MUST be fallback-eligible (never a bare [DONE])"
    )
    # The invariant holds both ways:
    #   (a) chain present -> _ComboFallbackNeeded raised (no client frames at all)
    #   (b) chain exhausted -> terminal frame + [DONE], never bare [DONE] 200.
    exhausted_tail = [
        sse_data(terminal_error_frame(504, "upstream_idle_timeout", "test-model")),
        SSE_DONE,
    ]
    assert b"finishReason" in exhausted_tail[0]
    assert exhausted_tail[1] == b": [DONE]\n\n"


def test_keepalive_ticks_below_cap_yield_comments():
    """REGRESSION: keepalive ticks below the idle cap still yield `: keepalive`
    comments, and a stream whose chunks keep arriving (gaps < cap) never trips
    the cap — healthy long-thinking streams are unaffected."""
    drain, _SENTINEL = _make_drain_loop_with_stats(max_idle=0.2, keepalive_interval=0.01)

    async def slow_thinker():
        # Chunks every ~0.05s — each is real data that resets the timer, so
        # the 0.2s cap must never fire between them.
        for _ in range(4):
            yield b'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n'
            await asyncio.sleep(0.05)

    async def run():
        return await drain(slow_thinker)

    emitted, stats = asyncio.run(run())
    keepalives = [c for c in emitted if isinstance(c, bytes) and c == b": keepalive\n\n"]
    idle_events = [c for c in emitted if isinstance(c, tuple) and c[0] == "IDLE_TIMEOUT"]
    assert keepalives, "sub-cap ticks must still yield : keepalive comments"
    assert idle_events == [], f"healthy chunk cadence must never trip the cap: {idle_events}"
    assert sum(1 for c in emitted if isinstance(c, (bytes, bytearray)) and b'"content":"tok"' in c) == 4
    assert stats["error"] is None
    assert stats["status"] == 200


# ─── 2026-10-02 production source pins (the fix is IN app/main.py) ──────────


def _pass_source() -> str:
    """Source of _process_chat_completion_pass — every egress lane (gemini,
    anthropic, anthropic-to-openai, openai) is nested inside it, so one
    getsource covers all sites. Pins by TEXT, never by line number."""
    import inspect

    return inspect.getsource(main._process_chat_completion_pass)


def test_production_idle_site_sets_504_before_break():
    """PIN: the Gemini idle-expiry site in app/main.py must set
    stats["status"] = 504 AFTER the upstream_idle_timeout error assignment and
    BEFORE the break — the exact ordering that routes the post-drain branch."""
    src = _pass_source()
    anchor = 'stats["error"] = stats.get("error") or "upstream_idle_timeout"'
    idx = 0
    found = False
    while True:
        idx = src.find(anchor, idx)
        if idx == -1:
            break
        window = src[idx: idx + 400]
        err_line = 'stats["error"] = stats.get("error") or "upstream_idle_timeout"'
        status_line = 'stats["status"] = 504'
        break_kw = "break"
        i_err = window.find(err_line)
        i_status = window.find(status_line)
        i_break = window.find(break_kw, i_err)
        if i_err != -1 and i_status != -1 and i_break != -1:
            assert i_err < i_status < i_break, (
                f"idle site must set error, then status 504, then break; got "
                f"err@{i_err} status@{i_status} break@{i_break}"
            )
            found = True
        idx += len(anchor)
    assert found, "the upstream_idle_timeout site(s) must exist in the pass source"


def test_production_stall_tuples_include_idle_error():
    """PIN: every post-drain stall tuple (gemini egress, openai egress,
    anthropic egress, anthropic-to-openai egress) must match
    "upstream_idle_timeout" so an idle break never falls through to a bare
    [DONE]. The pre-fix file had four bare two-element tuples."""
    src = _pass_source()
    legacy = src.count('in ("ttft_stall", "stream_stall"):')
    assert legacy == 0, (
        f"{legacy} stall tuple(s) still lack upstream_idle_timeout — an idle "
        "break there would end in a bare [DONE] with status 200"
    )
    extended = src.count('in ("ttft_stall", "stream_stall", "upstream_idle_timeout"):')
    assert extended >= 4, (
        f"expected the four post-drain stall branches to be extended, found {extended}"
    )


# ─── BUG B: _backfill_missing_usage must fire on the Gemini path too ───────


def test_backfill_fires_when_usage_omitted_and_text_delivered():
    """MintRouter omits the terminal usage frame. The Gemini path's inline
    reader leaves stats at In:0/Out:0 for a HEALTHY stream. The backfill
    helper must estimate non-zero tokens from the delivered text so the log
    tells the truth and the zero-output guard below does not mis-fire."""
    stats = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
    messages = [{"role": "user", "content": "Explain quantum tunneling in two paragraphs."}]
    delivered = (
        "Quantum tunneling is a phenomenon where a particle passes through a "
        "potential barrier it classically should not. It arises from the wave "
        "nature of matter and underlies stellar fusion and scanning tunneling microscopy."
    )
    main._backfill_missing_usage(
        stats, messages, delivered,
        provider_name="mintrouter", target_model="claude-opus-5",
    )
    assert stats["out"] >= 1, "out must be estimated from delivered text"
    assert stats["out"] >= len(delivered) // 4 - 1  # ~4 chars/token floor
    assert stats.get("usage_estimated") is True


def test_backfill_leaves_real_usage_untouched():
    """A genuine usage frame always wins: if stats already carry in/out, the
    helper returns early and never estimates. This is what keeps a real
    upstream usage report from being overwritten on the Gemini path."""
    stats = {"in": 1234, "out": 567, "cached": 0, "cache_write": 0}
    messages = [{"role": "user", "content": "hi"}]
    delivered = "a long answer that would otherwise estimate a large out count"
    main._backfill_missing_usage(stats, messages, delivered, provider_name="x", target_model="y")
    assert stats["in"] == 1234
    assert stats["out"] == 567
    assert "usage_estimated" not in stats


def test_backfill_does_not_mask_dead_leaf():
    """A genuine dead leaf (empty partial_text) must NOT be masked: out==0 is
    truthful there, and the zero-output combo fallback must still fire. This
    is the contract that lets the Gemini path call backfill unconditionally
    before the zero-token guard without hiding dead leaves."""
    stats = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
    main._backfill_missing_usage(
        stats, [{"role": "user", "content": "hi"}], "",
        provider_name="x", target_model="y",
    )
    assert stats["out"] == 0
    assert stats["in"] == 0
    assert "usage_estimated" not in stats
