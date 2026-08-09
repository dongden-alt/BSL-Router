"""Anti-freeze kill-registry + auto-recovery helpers.

Layers:
1. ACTIVE_STREAMS registry: every live SSE stream generator task is
   registered here so a force-stop endpoint can cancel it deterministically.
2. force_stop_all(): cancel registered tasks; each wrapper emits a terminal
   error + [DONE] frame while unwinding, so the IDE client unblocks instead of
   waiting forever (the 400/503/504 freeze).
3. bench_leaf(): records an upstream outcome so error_prevention auto-heals
   (bans the dead leaf; the next combo request skips it) on ALL terminal error
   paths — not just stalls.
4. stream_deadline(): hard total wall-clock cap per stream. Any generator that
   exceeds the deadline is force-terminated with a terminal frame.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator, Dict, Optional

# Registered active stream tasks: stream_id -> asyncio.Task
ACTIVE_STREAMS: Dict[str, asyncio.Task] = {}
# Forensic only (2026-08-04): stream_id -> monotonic registration time, so
# unregister can report how long a stream lived. Popped in lockstep with
# ACTIVE_STREAMS so it cannot outlive the registry it mirrors.
_STREAM_BIRTH: Dict[str, float] = {}
_STREAMS_LOCK = asyncio.Lock()
_stream_counter = 0

# 9ROUTER PARITY (2026-08-04): 0.0 = DISABLED. 9router imposes no wall-clock cap
# on a stream; only the upstream ends it.
#
# The old 600s cap force-closed any stream living longer than 10 minutes on the
# assumption that it was "a stuck leaf". That assumption is wrong for agentic
# work: a long tool-using turn with extended thinking can legitimately exceed 10
# minutes, and force-closing it mid-flight is itself a freeze source (the client
# receives a synthetic error frame in place of real content).
#
# NOTE: `stream_deadline` below now treats <= 0 as "no deadline". Both changes
# are required together -- zeroing this constant WITHOUT that guard would make
# `remaining <= 0` true on the first iteration and force-close every stream
# immediately.
#
# 2026-08-09: Re-enabled at 600s (10 minutes). Long agentic reasoning chains
# (DeepSeek V4, MiniMax M3 with extended thinking) can legitimately run 5-8
# minutes. The 10-minute cap catches truly stuck streams while preserving
# legitimate long-running tool-use turns. If a stream hits this deadline,
# the client receives a synthetic error frame instead of hanging forever.
STREAM_HARD_DEADLINE_SECONDS = 600.0


def next_stream_id() -> str:
    global _stream_counter
    _stream_counter += 1
    return f"stream-{_stream_counter}-{time.time():.0f}"


async def register_stream(stream_id: str, task: asyncio.Task) -> None:
    async with _STREAMS_LOCK:
        ACTIVE_STREAMS[stream_id] = task
        _STREAM_BIRTH[stream_id] = time.monotonic()
    # FORENSIC (2026-08-04): the heartbeat only reports a COUNT, so a climbing
    # active_streams tells us a leak exists but not WHICH stream leaked. Log
    # identity on both edges; an id that registers and never unregisters is the
    # leak, and its absence from the unregister log is the proof.
    print(
        f"[AFZ-STREAM] +register {stream_id} active={len(ACTIVE_STREAMS)}",
        flush=True,
    )


async def unregister_stream(stream_id: str) -> None:
    async with _STREAMS_LOCK:
        ACTIVE_STREAMS.pop(stream_id, None)
        _born = _STREAM_BIRTH.pop(stream_id, None)
    _age = f"{time.monotonic() - _born:.1f}s" if _born is not None else "unknown"
    print(
        f"[AFZ-STREAM] -unregister {stream_id} age={_age} active={len(ACTIVE_STREAMS)}",
        flush=True,
    )


async def force_stop_all() -> int:
    """Cancel every active stream task. Returns the number cancelled."""
    async with _STREAMS_LOCK:
        tasks = list(ACTIVE_STREAMS.values())
        _ids = list(ACTIVE_STREAMS.keys())
        ACTIVE_STREAMS.clear()
        # Clear in lockstep: leaving entries here would turn the forensic map
        # into an unbounded leak of its own across repeated force-stops.
        _STREAM_BIRTH.clear()
    cancelled = 0
    for t in tasks:
        if not t.done():
            t.cancel()
            cancelled += 1
    if cancelled:
        print(f"[AntiFreeze] force-stop cancelled {cancelled} active stream(s): {_ids}", flush=True)
    return cancelled


def active_stream_count() -> int:
    return len(ACTIVE_STREAMS)


def bench_leaf(config: Any, provider: str, model: str, status: int, err: Optional[str], out_tokens: int = 0) -> None:
    """Auto-heal: record an upstream failure so error_prevention bans/cooldowns
    the leaf and subsequent combo requests skip it. Never raises."""
    if status is not None and status < 400 and not err:
        return
    if out_tokens and out_tokens > 0:
        return  # user got tokens; not a dead leaf
    try:
        import app.error_prevention as _ep
        _ep.record_outcome(
            config, provider, model,
            status if isinstance(status, int) and status >= 400 else 502,
            err or "stream_failed",
            out_tokens=out_tokens,
        )
        print(
            f"[AntiFreeze] benched leaf {provider}/{model} "
            f"(status={status}, err={str(err)[:120]})",
            flush=True,
        )
    except Exception as _be:
        print(f"[AntiFreeze] bench failed (non-blocking): {_be}", flush=True)


async def stream_deadline(
    agen: AsyncGenerator[bytes, None],
    stream_id: str,
    deadline_s: float = STREAM_HARD_DEADLINE_SECONDS,
    error_frame: bytes = b'data: {"error": {"message": "stream force-stopped", "type": "proxy_error"}}\n\n',
    done_frame: bytes = b"data: [DONE]\n\n",
    protocol: str = "openai",
):
    """Wrap a stream with a hard wall-clock deadline, including blocked reads.

    FREEZE FIX (2026-08-04) — `protocol`
    The default error_frame/done_frame pair above is OPENAI-shaped. Emitting it
    to a GEMINI client does not end the stream: the Gemini parser terminates on
    a candidate carrying `finishReason`, ignores `{"error": ...}` (no
    `candidates`), and has no `[DONE]` sentinel at all. So a hard-deadline
    force-close on a Gemini stream printed "force-closing" server-side while the
    IDE kept waiting forever — a freeze that looks identical to the upstream
    502/503/504 freeze but originates HERE, in the guard meant to prevent it.

    Pass protocol="gemini" for Gemini egress so the terminal frames match the
    wire format the client is actually parsing.
    """
    if protocol == "gemini":
        # Imported lazily: antifreeze must not hard-depend on the adapter layer.
        try:
            from app.compat.adapters.gemini import sse_data as _g_sse, terminal_error_frame as _g_term
            error_frame = _g_sse(_g_term(504, f"stream force-stopped after {deadline_s}s"))
        except Exception:
            pass  # Fail-open: keep the OpenAI frames rather than break the stream.

    loop = asyncio.get_running_loop()
    iterator = agen.__aiter__()

    # 9ROUTER PARITY: no deadline -> forward bytes untouched. This is the whole
    # pump, matching 9router's `for(;;){ read(); write(); }`. No wait_for, no
    # clock arithmetic, no synthetic terminal frames on a healthy stream.
    if deadline_s is None or deadline_s <= 0:
        try:
            async for chunk in iterator:
                yield chunk
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await agen.aclose()
            except BaseException:
                pass
        return

    deadline = loop.time() + deadline_s
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                print(
                    f"[AntiFreeze] {stream_id} exceeded hard deadline ({deadline_s}s) — force-closing.",
                    flush=True,
                )
                try:
                    yield error_frame
                    yield done_frame
                except Exception:
                    pass
                return
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                print(
                    f"[AntiFreeze] {stream_id} exceeded hard deadline ({deadline_s}s) — force-closing.",
                    flush=True,
                )
                try:
                    yield error_frame
                    yield done_frame
                except Exception:
                    pass
                return
            yield chunk
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await agen.aclose()
        except BaseException:
            pass


async def afz_guard(
    gen: AsyncGenerator[bytes, None],
    stream_id: str,
    protocol: str = "openai",
    **deadline_kwargs,
) -> AsyncGenerator[bytes, None]:
    """Register the stream, forward it, and GUARANTEE a terminal frame.

    THE 9ROUTER FREEZE BUG, FIXED HERE (2026-08-04)
    ----------------------------------------------
    9router's fetch-path pump (`function Mc` in its mitm/server.js) is:

        let i = e.body.getReader();
        for (;;) { let {done, value} = await i.read();
                   if (done) { t.end(); break; }
                   t.write(...); }

    There is NO try/catch around it. If `read()` REJECTS mid-stream -- upstream
    502, socket reset, TLS teardown -- the exception propagates out and `t.end()`
    is never reached. The HTTP response is left open forever and the IDE waits
    forever. That is the single freeze 9router still has, and it needs a
    MID-stream death (not a pre-flight failure), which is why it is infrequent.

    Notably 9router's own RAW passthrough paths handle this correctly:

        u.on("error", l => { t.headersSent || t.writeHead(502);
                             t.headersSent ? t.destroy(l) : t.end(...); });

    We copy the CORRECT pattern, not the buggy one, and make it structural: this
    is the single chokepoint every client-facing StreamingResponse passes
    through, so no individual call site can forget it.

    CONTRACT
      - Bytes already sent  -> emit a protocol-correct terminal frame, then close.
      - No bytes sent yet   -> emit an error frame + terminal frame, then close.
      - Cancellation (Stop) -> re-raise untouched; the client is already gone and
                               writing to it would mask the cancellation.
    In every non-cancelled case the client receives an end-of-stream marker it
    can actually parse, so a dead upstream can never present as a hang.
    """
    _task = asyncio.current_task()
    await register_stream(stream_id, _task)
    _inner = stream_deadline(gen, stream_id, protocol=protocol, **deadline_kwargs)
    _emitted = False
    try:
        async for chunk in _inner:
            if chunk:
                _emitted = True
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        # Client-initiated stop, server shutdown, or generator close.
        #
        # BUG FIXED HERE (2026-08-04, caught by 3 cancellation tests): the first
        # version of this guard caught bare `BaseException`, which SWALLOWS
        # GeneratorExit. Two things then go wrong at once:
        #   1. Yielding inside a GeneratorExit handler is illegal -- Python
        #      raises RuntimeError("async generator ignored GeneratorExit"),
        #      surfacing as a stray StopAsyncIteration.
        #   2. The `finally` chain that closes the UPSTREAM httpx response never
        #      runs cleanly, leaking the very socket this module exists to free.
        #
        # Both must propagate untouched. There is no client left to receive a
        # terminal frame -- it hung up, which is precisely why we are here.
        raise
    except BaseException as _err:

        # Upstream died mid-flight (RemoteProtocolError, ReadError, reset, ...).
        # Emitting the terminal frame is the whole point of this guard.
        print(
            f"[AntiFreeze] {stream_id} upstream failed mid-stream "
            f"({type(_err).__name__}: {_err}) -- emitting terminal frame "
            f"(emitted={_emitted}, protocol={protocol}).",
            flush=True,
        )
        try:
            for _frame in _terminal_frames(protocol, _err, emitted=_emitted):
                yield _frame
        except BaseException:
            pass  # Never let cleanup raise over the original failure.
    finally:
        # LEAK FIX (2026-08-02): close the inner generator explicitly so a
        # downstream aclose() propagates through stream_deadline into the
        # egress generator's finally (which closes the upstream response).
        try:
            await _inner.aclose()
        except BaseException:
            pass
        await unregister_stream(stream_id)


def _terminal_frames(protocol: str, err: BaseException, emitted: bool) -> list[bytes]:
    """Build an end-of-stream marker the CLIENT's parser will actually accept.

    Protocol matters more than it appears. A Gemini client terminates on a
    candidate carrying `finishReason`; it ignores an OpenAI-shaped
    `{"error": ...}` (no `candidates`) and has no `[DONE]` sentinel at all. So
    emitting OpenAI frames to a Gemini client is indistinguishable from emitting
    nothing -- a freeze produced by the very code meant to prevent one. This bug
    was hit for real on 2026-08-04; see `stream_deadline`'s `protocol` argument.
    """
    _msg = f"upstream stream failed: {type(err).__name__}"
    if protocol == "gemini":
        try:
            from app.compat.adapters.gemini import (
                sse_data as _g_sse,
                terminal_error_frame as _g_term,
            )
            return [_g_sse(_g_term(502, _msg))]
        except Exception:
            pass  # Fall through to the OpenAI shape rather than emit nothing.
    if protocol == "anthropic":
        # Anthropic clients terminate on message_stop; `[DONE]` is not part of
        # the wire format and an unterminated message_delta leaves them waiting.
        return [
            b'event: error\ndata: {"type": "error", "error": {"type": "api_error", '
            + b'"message": "' + _msg.encode("utf-8", "replace") + b'"}}\n\n',
            b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
        ]
    _frames: list[bytes] = []
    if not emitted:
        # Nothing reached the client yet, so an error object is still parseable
        # as the whole response rather than corrupting a partial message.
        _frames.append(
            b'data: {"error": {"message": "' + _msg.encode("utf-8", "replace")
            + b'", "type": "proxy_error"}}\n\n'
        )
    _frames.append(b"data: [DONE]\n\n")
    return _frames

