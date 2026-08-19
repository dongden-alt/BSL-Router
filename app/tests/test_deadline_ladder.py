"""Stream-deadline retry ladder tests.

Proves:
  1. _ladder_deadline returns 600/300/600/600 for attempts 0,1,2,3+
  2. Pre-emission blocked pump raises _DeadlineRetryNeeded (single path)
  3. Pre-emission blocked combo raises _ComboFallbackNeeded with next idx
  4. Post-emission blocked pump does not retry/fallback
  5. Iterator aclose is called on expiry
  6. CancelledError propagates untouched
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


def test_ladder_deadline_helper():
    main = _reload_main()
    assert main.STREAM_DEADLINE_LADDER == (600.0, 300.0, 600.0)
    assert main._ladder_deadline(0) == 600.0
    assert main._ladder_deadline(1) == 300.0
    assert main._ladder_deadline(2) == 600.0
    assert main._ladder_deadline(3) == 600.0
    assert main._ladder_deadline(99) == 600.0
    assert main._ladder_deadline(-1) == 600.0


class _BlockedIter:
    """Async iterator that never yields; hangs until cancelled/timeout."""

    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class _CancelIter:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise asyncio.CancelledError()


def test_pre_emission_single_raises_deadline_retry():
    main = _reload_main()
    from app.middleware.stream_guard import StreamEmissionState

    emit = StreamEmissionState()
    stats = {"error": None, "status": 200, "out": 0}
    it = _BlockedIter()

    async def _run():
        with pytest.raises(main._DeadlineRetryNeeded) as ei:
            async for _ in main._deadline_stall_pump(
                it,
                attempt=0,
                deadline_s=0.05,
                emit=emit,
                stats=stats,
                label="test",
                active_chain=None,
                retry_state=None,
            ):
                pass
        return ei.value

    err = asyncio.run(_run())
    assert err.attempt == 0
    assert stats["error"] and "deadline_stall_attempt1" in stats["error"]
    assert it.closed is True


def test_pre_emission_combo_raises_combo_fallback():
    main = _reload_main()
    from app.middleware.stream_guard import StreamEmissionState

    emit = StreamEmissionState()
    stats = {"error": None, "status": 200, "out": 0}
    chain = ["a/m1", "b/m2"]
    it = _BlockedIter()

    async def _run():
        with pytest.raises(main._ComboFallbackNeeded) as ei:
            async for _ in main._deadline_stall_pump(
                it,
                attempt=0,
                deadline_s=0.05,
                emit=emit,
                stats=stats,
                label="test",
                active_chain=chain,
                retry_state={"idx": 0, "chain": chain},
                cache_bp={},
                original_model="combo",
                chain_deadline=time.monotonic() + 100.0,
                chain_budget_remaining=lambda: 50.0,
                model="combo",
                target_model="m1",
                provider_name="a",
            ):
                pass
        return ei.value

    err = asyncio.run(_run())
    assert err.status_code == 504
    assert err.retry_state["idx"] == 1
    assert it.closed is True


def test_post_emission_no_retry():
    main = _reload_main()
    from app.middleware.stream_guard import StreamEmissionState

    emit = StreamEmissionState()
    emit.mark_emitted(b"already-sent")
    stats = {"error": None, "status": 200, "out": 5}
    it = _BlockedIter()

    async def _run():
        chunks = []
        async for c in main._deadline_stall_pump(
            it,
            attempt=0,
            deadline_s=0.05,
            emit=emit,
            stats=stats,
            label="test",
            active_chain=["a", "b"],
            retry_state={"idx": 0},
            chain_budget_remaining=lambda: 50.0,
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())
    assert chunks == []
    assert stats["error"] and stats["error"].startswith("stream_deadline_")
    assert it.closed is True


def test_iterator_close_called():
    main = _reload_main()
    from app.middleware.stream_guard import StreamEmissionState

    emit = StreamEmissionState()
    stats = {"error": None, "status": 200, "out": 0}
    it = _BlockedIter()

    async def _run():
        with pytest.raises(main._DeadlineRetryNeeded):
            async for _ in main._deadline_stall_pump(
                it, attempt=1, deadline_s=0.05, emit=emit, stats=stats
            ):
                pass

    asyncio.run(_run())
    assert it.closed is True


def test_cancelled_error_propagates():
    main = _reload_main()
    from app.middleware.stream_guard import StreamEmissionState

    emit = StreamEmissionState()
    stats = {"error": None, "status": 200, "out": 0}

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            async for _ in main._deadline_stall_pump(
                _CancelIter(),
                attempt=0,
                deadline_s=1.0,
                emit=emit,
                stats=stats,
            ):
                pass

    asyncio.run(_run())


def test_ladder_exhausted_returns_without_raise():
    main = _reload_main()
    from app.middleware.stream_guard import StreamEmissionState

    emit = StreamEmissionState()
    stats = {"error": None, "status": 200, "out": 0}
    it = _BlockedIter()

    async def _run():
        chunks = []
        async for c in main._deadline_stall_pump(
            it,
            attempt=len(main.STREAM_DEADLINE_LADDER) - 1,
            deadline_s=0.05,
            emit=emit,
            stats=stats,
            active_chain=None,
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())
    assert chunks == []
    assert stats["error"] and "deadline_stall_attempt" in stats["error"]
    assert it.closed is True
