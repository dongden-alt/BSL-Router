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
