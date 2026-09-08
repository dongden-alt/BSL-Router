"""Lane 3 — accept-task exception retrieval tests (2026-09-07).

Root cause proven from CPython 3.10 source (windows_events.py,
IocpProactor.accept): every accept arm schedules a SHADOW task —

    async def accept_coro(future, conn):
        # Coroutine closing the accept socket if the future is cancelled
        try:
            await future
        except exceptions.CancelledError:
            conn.close()
            raise

    future = self._register(ov, listener, finish_accept)
    coro = accept_coro(future, conn)
    tasks.ensure_future(coro, loop=self._loop)   # <- fire-and-forget
    return future

`accept_coro` re-raises every non-cancellation exception into a task nobody
references. The F8-A guard retrieves the FUTURE's exception via f.result(),
but the shadow TASK's copy stayed unretrieved -> asyncio's Task.__del__
printed "Task exception was never retrieved" + the full WinError-64 traceback
on every accept-loop resurrection (app.err.log, 1.7MB).

Fix under test (app.dualstack_serve):
  - _consume_done_task: done-callback that retrieves+swallows the exception
    (cancelled tasks are normal and stay silent; audit lines are throttled).
  - _track_shadow_accept_tasks: finds live accept_coro tasks by coroutine
    qualname and attaches the callback (idempotent via WeakSet).
  - Wiring: both arm sites of the F8-A guard call the tracker.

The asyncio behavior is simulated directly with low-level primitives (there
are no existing guard-specific dualstack tests to mirror; conventions follow
test_dualstack_bind_retry.py / test_dualstack_instance_lock.py).
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import dualstack_serve as ds  # noqa: E402


# ── Harness: observe the "Task exception was never retrieved" pathway ────────
#
# When a task dies with an unretrieved exception, Task.__del__ calls
# loop.call_exception_handler(context) with context["message"] set to exactly
# that string. We install a recording handler and assert on it — the same
# channel the stderr spam flows through.


class _HandlerRecorder:
    def __init__(self):
        self.records = []

    def __call__(self, loop, context):
        self.records.append(context)


def _never_retrieved_warnings(recorder):
    return [
        c for c in recorder.records
        if "never retrieved" in str(c.get("message", ""))
    ]


async def _spawn_dying_task(exc: BaseException) -> asyncio.Task:
    async def _boom():
        raise exc

    return asyncio.ensure_future(_boom())


async def _let_task_del_run(task: asyncio.Task) -> None:
    """Await the task, then yield several loop iterations so __del__ /
    call_exception_handler scheduling settles deterministically."""
    try:
        await asyncio.shield(task)
    except BaseException:
        pass
    for _ in range(6):
        await asyncio.sleep(0)


# ── 1. _consume_done_task retrieves the exception -> no warning ──────────────


def test_consume_done_task_silences_unretrieved_exception():
    recorder = _HandlerRecorder()

    async def _run():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        loop.set_exception_handler(recorder)
        try:
            task = await _spawn_dying_task(OSError(22, "netname gone", None, 64, None))
            task.add_done_callback(ds._consume_done_task)
            await _let_task_del_run(task)
        finally:
            loop.set_exception_handler(old)

    asyncio.run(_run())
    assert _never_retrieved_warnings(recorder) == [], (
        "exception was retrieved by _consume_done_task — asyncio must not log "
        "'Task exception was never retrieved'"
    )


def test_unretrieged_control_case_still_warns():
    """Control: WITHOUT the consuming callback the warning pathway fires.
    Proves the recorder actually detects the failure mode being fixed.

    The dying task must NEVER be awaited or .exception()'d — any retrieval
    clears the warning. The task's local ref is dropped after completion so
    CPython refcounting runs Task.__del__ deterministically, which is what
    calls loop.call_exception_handler({'message': 'Task exception was never
    retrieved', ...})."""
    recorder = _HandlerRecorder()

    async def _run():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        loop.set_exception_handler(recorder)
        try:
            task = await _spawn_dying_task(OSError(22, "netname gone", None, 64, None))
            for _ in range(4):
                await asyncio.sleep(0)  # let the task run to completion
            assert task.done() and not task.cancelled()
            del task  # last strong ref -> __del__ -> unretrieved-exception warning
            for _ in range(6):
                await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(old)

    asyncio.run(_run())
    assert len(_never_retrieved_warnings(recorder)) >= 1, (
        "control case must produce a 'never retrieved' warning, otherwise the "
        "recorder is not wired to the real failure channel"
    )


def test_consume_done_task_cancelled_task_is_normal():
    """Cancelled accept tasks are the NORMAL path (client disconnects, loop
    close) — the callback must stay silent and never raise."""
    recorder = _HandlerRecorder()

    async def _run():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        loop.set_exception_handler(recorder)
        try:
            async def _hang():
                await asyncio.sleep(3600)

            task = asyncio.ensure_future(_hang())
            task.add_done_callback(ds._consume_done_task)
            await asyncio.sleep(0)
            task.cancel()
            await _let_task_del_run(task)
        finally:
            loop.set_exception_handler(old)

    asyncio.run(_run())
    assert _never_retrieved_warnings(recorder) == []
    assert ds._consume_done_task  # callback reference still valid


def test_consume_done_task_clean_result_is_silent():
    recorder = _HandlerRecorder()

    async def _run():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        loop.set_exception_handler(recorder)
        try:
            async def _ok():
                return 42

            task = asyncio.ensure_future(_ok())
            task.add_done_callback(ds._consume_done_task)
            await _let_task_del_run(task)
        finally:
            loop.set_exception_handler(old)

    asyncio.run(_run())
    assert recorder.records == []


def test_consume_done_task_swallows_future_cancelled_check():
    """A task cancelled while pending makes .exception() itself raise
    CancelledError — the callback's guard must convert that to silence."""
    recorder = _HandlerRecorder()

    async def _run():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        loop.set_exception_handler(recorder)
        try:
            task = await _spawn_dying_task(OSError(22, "x", None, 64, None))
            task.add_done_callback(ds._consume_done_task)
            await _let_task_del_run(task)
            # Re-attaching on an already-consumed task must also stay silent.
            ds._consume_done_task(task)
        finally:
            loop.set_exception_handler(old)

    asyncio.run(_run())
    assert _never_retrieved_warnings(recorder) == []


# ── 2. _track_shadow_accept_tasks finds accept_coro tasks ────────────────────


def test_track_shadow_accept_tasks_attaches_and_is_idempotent():
    recorder = _HandlerRecorder()

    async def _run():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        loop.set_exception_handler(recorder)
        try:
            # A task whose coroutine qualname leaf is 'accept_coro' — the exact
            # CPython shadow-task shape.
            async def accept_coro():
                raise OSError(22, "netname gone", None, 64, None)

            shadow = asyncio.ensure_future(accept_coro())
            ds._TRACKED_ACCEPT_TASKS.clear()
            ds._track_shadow_accept_tasks()
            # Idempotent: a second scan while still pending must not
            # double-attach (WeakSet membership).
            ds._track_shadow_accept_tasks()
            await _let_task_del_run(shadow)
        finally:
            loop.set_exception_handler(old)

    asyncio.run(_run())
    assert _never_retrieved_warnings(recorder) == [], (
        "tracked accept_coro shadow task's exception must be consumed"
    )


def test_track_shadow_accept_tasks_ignores_other_coroutines():
    """Tasks that are not accept shadows must NOT gain the consuming
    callback (their exceptions belong to their own error handling)."""

    async def _run():
        async def ordinary_boom():
            raise ValueError("belongs to its own handler")

        task = asyncio.ensure_future(ordinary_boom())
        ds._TRACKED_ACCEPT_TASKS.clear()
        ds._track_shadow_accept_tasks()
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        assert task not in ds._TRACKED_ACCEPT_TASKS
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(_run())


def test_track_shadow_accept_tasks_safe_without_running_loop():
    # Called outside a loop (e.g. from a non-async context) -> no-op, no raise.
    ds._TRACKED_ACCEPT_TASKS.clear()
    ds._track_shadow_accept_tasks()  # must not raise RuntimeError
    assert len(ds._TRACKED_ACCEPT_TASKS) == 0


# ── 3. Guard wiring: arm sites call the tracker ───────────────────────────────


def test_guard_arm_sites_call_shadow_tracker():
    """Source-level lock: both accept-arm sites inside the F8-A guard
    (_start_serving wrapper) must invoke _track_shadow_accept_tasks, so a
    future refactor cannot silently drop the wiring."""
    src = (Path(ROOT) / "app" / "dualstack_serve.py").read_text(encoding="utf-8")
    guard_src = src.split("def _guard_start_serving(")[1]
    calls = guard_src.count("_track_shadow_accept_tasks()")
    assert calls >= 2, (
        f"expected the tracker at BOTH arm sites (initial arm + re-arm), "
        f"found {calls} call(s)"
    )


def test_module_exposes_retrieval_constants():
    # Sanity: the module-level contract exists and the throttle is sane.
    assert hasattr(ds, "_consume_done_task")
    assert hasattr(ds, "_track_shadow_accept_tasks")
    assert hasattr(ds, "_TRACKED_ACCEPT_TASKS")
    assert ds._ACCEPT_TASK_EXCEPTION_LOG_EVERY >= 1


# ── 4. Audit throttle counters ───────────────────────────────────────────────


def test_consume_done_task_counts_and_throttles_audit(monkeypatch):
    lines = []
    monkeypatch.setattr(ds, "_audit_line", lambda line: lines.append(line))
    # Fresh counter: the throttle asserts against n=1..4 of THIS batch
    # (earlier tests may have consumed exceptions already).
    monkeypatch.setattr(ds, "_accept_task_exceptions_consumed", 0)
    try:
        exc = OSError(22, "netname gone", None, 64, None)

        async def _run():
            for i in range(4):
                task = await _spawn_dying_task(exc)
                task.add_done_callback(ds._consume_done_task)
                try:
                    await asyncio.shield(task)
                except BaseException:
                    pass
                for _ in range(2):
                    await asyncio.sleep(0)

        asyncio.run(_run())
        consumed = ds._accept_task_exceptions_consumed
        assert consumed == 4
        # First 3 of the batch log (n<=3); the 4th is throttled (4 % 50 != 0).
        new_lines = [ln for ln in lines if "ACCEPT_TASK_EXCEPTION_CONSUMED" in ln]
        assert len(new_lines) == 3, f"throttle misfire: {new_lines!r}"
        assert "OSError" in new_lines[0]
    finally:
        pass  # monkeypatch restores the counter and _audit_line
