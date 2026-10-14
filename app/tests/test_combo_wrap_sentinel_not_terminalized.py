"""Regression: internal control-flow sentinels must NOT be terminalized as 502.

Bug (2026-10-14): a wrapped/re-raised `_ComboWrapRestart` escaping its ROOT-loop
catcher landed in afz_guard's catch-all `except BaseException`, which converted
it into a client-visible 502:

    [BSL Router] upstream error 502: upstream stream failed: _ComboWrapRestart

The sentinel contract says it NEVER reaches a client. afz_guard must re-raise
internal sentinels untouched and only emit terminal frames for genuine upstream
transport failures. `_terminal_frames` must additionally sanitize its message so
raw httpx reprs and sentinel class names never leak.
"""
import asyncio

import pytest

from app import antifreeze


class _ComboWrapRestart(Exception):
    """Mirror of app.main's sentinel (import-free to avoid main.py boot)."""


class _ComboFallbackNeeded(Exception):
    """Mirror of the chain-advance sentinel."""


def _make_stream_gen(raises: BaseException):
    async def _gen():
        yield b'data: {"partial": true}\n\n'
        raise raises

    return _gen()


async def _drain(gen) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in gen:
        out.append(chunk)
    return out


def test_afz_guard_reraises_combo_wrap_restart():
    """_ComboWrapRestart must propagate untouched, never become a 502 frame."""

    async def _run():
        gen = antifreeze.afz_guard(
            _make_stream_gen(_ComboWrapRestart("boom")), "t1", protocol="openai"
        )
        with pytest.raises(_ComboWrapRestart):
            await _drain(gen)

    asyncio.run(_run())


def test_afz_guard_reraises_combo_fallback_needed():
    async def _run():
        gen = antifreeze.afz_guard(
            _make_stream_gen(_ComboFallbackNeeded("boom")), "t2", protocol="openai"
        )
        with pytest.raises(_ComboFallbackNeeded):
            await _drain(gen)

    asyncio.run(_run())


def test_afz_guard_still_terminalizes_genuine_transport_error():
    """A real mid-stream transport death still produces a terminal frame."""
    try:
        import httpx

        err = httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )
    except Exception:  # pragma: no cover - httpx always present in this repo
        err = OSError("simulated transport death")

    async def _run():
        gen = antifreeze.afz_guard(_make_stream_gen(err), "t3", protocol="openai")
        return await _drain(gen)

    frames = asyncio.run(_run())
    blob = b"".join(frames)
    assert b"[DONE]" in blob, "terminal [DONE] marker must be emitted"
    assert b"RemoteProtocolError" not in blob, "raw exception class must not leak"
    assert b"incomplete chunked read" not in blob, "raw httpx detail must not leak"


def test_terminal_frames_sanitizes_transport_error():
    import httpx

    err = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    frames = antifreeze._terminal_frames("openai", err, emitted=False)
    blob = b"".join(frames)
    assert b"incomplete chunked read" not in blob
    assert b"RemoteProtocolError" not in blob
    assert b"upstream connection dropped mid-stream" in blob


def test_terminal_frames_sanitizes_combo_sentinel_name():
    err = _ComboWrapRestart("internal")
    frames = antifreeze._terminal_frames("openai", err, emitted=False)
    blob = b"".join(frames)
    assert b"_ComboWrapRestart" not in blob, "sentinel class name must not leak"


_MIDSTREAM_MSG = (
    "midstream_transport: peer closed connection without sending complete "
    "message body (incomplete chunked read)"
)


def test_sanitize_client_error_maps_exact_reported_502():
    """The verbatim user-reported 502 text must map to the stable sentence."""
    out = antifreeze.sanitize_client_error(_MIDSTREAM_MSG)
    assert out == "upstream connection dropped mid-stream"
    assert "incomplete chunked read" not in out
    assert "midstream_transport" not in out


def test_sanitize_client_error_passthrough_labels():
    """Legit short labels and sentinels must keep their existing mapping."""
    assert antifreeze.sanitize_client_error("_ComboWrapRestart") == "internal retry signal"
    assert antifreeze.sanitize_client_error("upstream_idle_timeout") == "upstream_idle_timeout"
    assert antifreeze.sanitize_client_error("zero_output_tokens") == "zero_output_tokens"
    assert antifreeze.sanitize_client_error("stream_stall") == "stream_stall"
    assert antifreeze.sanitize_client_error("Upstream error 429: rate limited") == \
        "Upstream error 429: rate limited"


def test_gemini_terminal_error_frame_sanitizes():
    from app.compat.adapters.gemini import terminal_error_frame

    frame = terminal_error_frame(502, _MIDSTREAM_MSG, "m")
    text = frame["response"]["candidates"][0]["content"]["parts"][0]["text"]
    assert "incomplete chunked read" not in text
    assert "midstream_transport" not in text
    assert "upstream connection dropped mid-stream" in text


def test_openai_terminal_error_frames_sanitizes():
    from app import main

    frames = main._openai_terminal_error_frames(_MIDSTREAM_MSG, "m", 502)
    blob = b"".join(frames)
    assert b"incomplete chunked read" not in blob
    assert b"midstream_transport" not in blob
    assert b"upstream connection dropped mid-stream" in blob


def test_anthropic_terminal_error_frames_sanitizes():
    from app import main

    frames = main._anthropic_terminal_error_frames(_MIDSTREAM_MSG, "m")
    blob = b"".join(frames)
    assert b"incomplete chunked read" not in blob
    assert b"midstream_transport" not in blob
    assert b"upstream connection dropped mid-stream" in blob
