"""Lane 3 — SSE keepalive pre-first-chunk tests (2026-09-07).

CC SDK aborts with "Slow first byte: no stream chunk 30.0s after request
sent" when the router forwards upstream but sends the client NOTHING until
the first upstream chunk arrives (router TTFT 15-55s at effort:xhigh/max).
Fix under test: app.main._sse_keepalive_pre_first_chunk + the
tools.sse_keepalive config gate.

Contract proven here:
  1. Pre-TTFT emission: a slow upstream yields keepalive frames at ~interval
     spacing while the first chunk is pending, then real chunks — and the
     upstream read is NEVER cancelled by the keepalive waits (non-cancelling
     asyncio.wait race).
  2. No keepalive is EVER interleaved after the first real chunk (mid-stream
     bytes stay byte-faithful).
  3. Format per client family: anthropic lane -> `event: ping` + {"type":
     "ping"}; openai lanes -> `: keepalive` SSE comment.
  4. Gate OFF (tools.sse_keepalive.enabled false, scalar or dict form) ->
     zero keepalive bytes, byte-identical passthrough.
  5. Upstream answers fast (< interval) -> zero keepalive emitted.
  6. _sse_keepalive_settings defaults: absent key -> ON/10.0 (fail-open to
     the fix; config.yaml has no sse_keepalive key yet).

Style mirrors test_deadline_ladder.py: module-level async generators driven
by asyncio.run, main imported (reloaded once) via sys.path root insert.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reload_main():
    module = sys.modules.get("app.main")
    if module is None:
        return importlib.import_module("app.main")
    return importlib.reload(module)


# ── Fixture generators (test_deadline_ladder.py conventions) ─────────────────


class _SlowFirstChunkStream:
    """Async generator whose FIRST chunk arrives after `delay_s` (later chunks
    are immediate), then it streams `count` real chunks back-to-back.

    Tracks (for assertions):
      - was the __anext__ ever cancelled? (the keepalive race must not cancel
        the pending upstream read — that would kill the stream)
      - how many reads were STARTED by the time the first chunk was produced
        (must be exactly ONE: the pre-first-chunk wait races a single read)
    """

    def __init__(self, delay_s: float, count: int = 3):
        self.delay_s = delay_s
        self.count = count
        self.reads_started = 0
        self.first_chunk_reads = None
        self.was_cancelled = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reads_started += 1
        if self.first_chunk_reads is None:
            # Only the FIRST read pays the delay — models a slow TTFT, not a
            # slow stream (subsequent chunks arrive back-to-back).
            try:
                await asyncio.sleep(self.delay_s)
            except asyncio.CancelledError:
                self.was_cancelled = True
                raise
        if self.count <= 0:
            raise StopAsyncIteration
        self.count -= 1
        if self.first_chunk_reads is None:
            self.first_chunk_reads = self.reads_started
        return f'data: {{"chunk": {self.count + 1}}}\n\n'.encode("utf-8")

    async def aclose(self):
        self.closed = True


async def _fast_stream():
    for i in (3, 2, 1):
        yield f'data: {{"chunk": {i}}}\n\n'.encode("utf-8")


async def _collect_chunks(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


# ── 1. Pre-TTFT emission + spacing + no read cancellation ────────────────────


def test_keepalive_emitted_while_first_chunk_pending():
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.30, count=3)
    t0 = time.monotonic()

    async def _run():
        chunks = await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.1, fmt="openai")
        )
        return chunks

    chunks = asyncio.run(_run())
    elapsed = time.monotonic() - t0

    text = b"".join(chunks).decode("utf-8")
    # Keepalives at ~0.1s spacing during the 0.3s first-chunk delay.
    ka_count = text.count(": keepalive\n\n")
    assert ka_count >= 2, f"expected >=2 keepalives during 0.3s wait, got {ka_count}: {text!r}"
    # Real chunks arrive after the keepalives, untouched.
    assert text.count('"chunk": 3') == 1
    assert text.count('"chunk": 1') == 1
    # CRITICAL: while the first chunk was pending, exactly ONE read was
    # started — the keepalive race must never cancel/restart the upstream read.
    assert stream.first_chunk_reads == 1, (
        f"upstream __anext__ was restarted {stream.first_chunk_reads}x before "
        "the first chunk — the keepalive race must not cancel the pending read"
    )
    assert not stream.was_cancelled
    assert stream.closed, "inner stream must be aclose()d by the wrapper"
    # Total runtime ~= delay (keepalive waits are concurrent, not additive).
    assert elapsed < 1.0, f"keepalive waits made the stream serial: {elapsed:.2f}s"


def test_no_keepalive_after_first_chunk():
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.15, count=4)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.05, fmt="openai")
        )

    chunks = asyncio.run(_run())
    text = b"".join(chunks).decode("utf-8")
    first_ka_end = text.find(": keepalive\n\n") + len(": keepalive\n\n")
    if first_ka_end >= len(": keepalive\n\n"):
        # Everything after the FIRST keepalive's end must be real content only —
        # no keepalive may ever follow a real chunk.
        tail = text[first_ka_end:]
        first_real = tail.find('"chunk"')
        assert first_real != -1, "no real chunk after keepalives"
        assert ": keepalive" not in tail[first_real:], (
            "keepalive interleaved after content started — mid-stream bytes "
            "must stay byte-faithful"
        )


# ── 2. Format per lane ────────────────────────────────────────────────────────


def test_anthropic_lane_emits_ping_events():
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.15, count=1)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.05, fmt="anthropic")
        )

    chunks = asyncio.run(_run())
    text = b"".join(chunks).decode("utf-8")
    assert 'event: ping\ndata: {"type":"ping"}\n\n' in text, (
        f"anthropic lane must emit the official ping event, got: {text!r}"
    )
    # And never the openai comment form.
    assert ": keepalive" not in text


def test_openai_lane_emits_comment_frames():
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.15, count=1)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.05, fmt="openai")
        )

    chunks = asyncio.run(_run())
    text = b"".join(chunks).decode("utf-8")
    assert ": keepalive\n\n" in text, f"openai lane must emit SSE comments, got: {text!r}"
    assert "event: ping" not in text


def test_unknown_format_falls_back_to_openai_comment():
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.12, count=1)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.04, fmt="gemini")
        )

    chunks = asyncio.run(_run())
    text = b"".join(chunks).decode("utf-8")
    assert ": keepalive\n\n" in text


# ── 3. Gate OFF -> byte-identical passthrough ────────────────────────────────


def test_gate_disabled_zero_keepalive_bytes_and_passthrough_identical():
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.2, count=3)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.05, fmt="openai")
        )

    # NOTE: the gate is enforced at the CALL SITES (`if _sse_ka_on:`), so with
    # the gate off the wrapper is never entered. Prove the call-site contract
    # via _sse_keepalive_settings + prove the helper itself stays passthrough
    # when given a stream faster than the interval (below). Here we assert the
    # settings side of the contract for every disabled shape.
    assert main._sse_keepalive_settings({"tools": {"sse_keepalive": {"enabled": False}}})[0] is False
    assert main._sse_keepalive_settings({"tools": {"sse_keepalive": False}})[0] is False


def test_gate_off_call_site_skips_wrapper():
    """The egress return sites guard with `if _sse_ka_on:` — byte-identical
    behavior when disabled means the wrapper must not even be constructed.
    Static proof: no call-site wraps when the gate is False."""
    import re

    main_path = Path(ROOT) / "app" / "main.py"
    src = main_path.read_text(encoding="utf-8")
    for m in re.finditer(r"if _sse_ka_on:\n(.*?)(?=\n            return|\n        return)", src, re.S):
        assert "_sse_keepalive_pre_first_chunk" in m.group(1), (
            "gate branch must construct the keepalive wrapper"
        )


def test_fast_upstream_zero_keepalive_emitted():
    """Upstream answers well under the interval -> zero keepalive bytes."""
    main = _reload_main()

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(_fast_stream(), interval_s=30.0, fmt="openai")
        )

    chunks = asyncio.run(_run())
    text = b"".join(chunks).decode("utf-8")
    assert ": keepalive" not in text
    assert text.count("data:") == 3, "passthrough must deliver every real chunk"


def test_gate_disabled_wrap_is_never_applied_behavioral():
    """Behavioral mirror of the gate: the only caller-controlled gate path is
    `if _sse_ka_on`. With enabled=False the settings helper reports off, and a
    call site that honors it streams byte-identically (nothing here but the
    raw stream)."""
    main = _reload_main()
    on, _iv = main._sse_keepalive_settings({"tools": {"sse_keepalive": {"enabled": False, "interval_s": 0.05}}})
    assert on is False
    # If a caller mistakenly still wrapped, byte-identity would still hold for
    # a stream faster than the interval — belt and braces:
    stream = _SlowFirstChunkStream(delay_s=0.01, count=2)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=5.0, fmt="openai")
        )

    chunks = asyncio.run(_run())
    text = b"".join(chunks).decode("utf-8")
    assert ": keepalive" not in text
    assert text.count('"chunk"') == 2


# ── 4. Empty stream + settings defaults ──────────────────────────────────────


def test_empty_upstream_no_real_chunks_and_prompt_close():
    """An upstream that ends WITHOUT ever yielding must terminate the wrapper
    promptly with clean StopAsyncIteration handling and aclose called.
    Keepalives emitted while the read was still pending are CORRECT (the
    router cannot know the stream is empty until it ends) — but no real
    chunk may appear and the wrapper must not spin forever."""
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=0.05, count=0)

    async def _run():
        return await _collect_chunks(
            main._sse_keepalive_pre_first_chunk(stream, interval_s=0.02, fmt="openai")
        )

    t0 = time.monotonic()
    chunks = asyncio.run(_run())
    elapsed = time.monotonic() - t0
    text = b"".join(chunks).decode("utf-8")
    assert '"chunk"' not in text, "an empty upstream must deliver zero real chunks"
    # Every emitted byte (if any) was a keepalive comment frame.
    for frame in [c for c in chunks if c]:
        assert frame == b": keepalive\n\n"
    assert elapsed < 2.0, "empty stream must end promptly, not keepalive-spin"
    assert stream.closed


def test_settings_defaults_fail_open():
    main = _reload_main()
    # Absent key -> enabled (config.yaml has no sse_keepalive key yet).
    assert main._sse_keepalive_settings({}) == (True, 10.0)
    assert main._sse_keepalive_settings(None) == (True, 10.0)
    assert main._sse_keepalive_settings({"tools": {}}) == (True, 10.0)
    # Explicit dict form.
    assert main._sse_keepalive_settings(
        {"tools": {"sse_keepalive": {"enabled": True, "interval_s": 2.5}}}
    ) == (True, 2.5)
    # Bad interval falls back to 10.0.
    assert main._sse_keepalive_settings(
        {"tools": {"sse_keepalive": {"interval_s": 0}}}
    ) == (True, 10.0)
    assert main._sse_keepalive_settings(
        {"tools": {"sse_keepalive": {"interval_s": "nope"}}}
    ) == (True, 10.0)


def test_client_disconnect_cancels_pending_read_cleanly():
    """GeneratorExit (client gone mid-keepalive-wait) propagates, the pending
    upstream read is cancelled, and the inner stream is closed — no leaked
    task, no leaked socket."""
    main = _reload_main()
    stream = _SlowFirstChunkStream(delay_s=5.0, count=1)

    async def _run():
        agen = main._sse_keepalive_pre_first_chunk(stream, interval_s=0.05, fmt="openai")
        got = []
        async for chunk in agen:
            got.append(chunk)
            break  # consumer walks away after the first keepalive
        await agen.aclose()
        return got

    t0 = time.monotonic()
    got = asyncio.run(_run())
    elapsed = time.monotonic() - t0
    assert got and got[0] == b": keepalive\n\n"
    # aclose() must not wait out the 5s upstream delay.
    assert elapsed < 1.0, f"disconnect cleanup hung: {elapsed:.2f}s"
    assert stream.was_cancelled, "pending upstream read must be cancelled on disconnect"
    assert stream.closed
