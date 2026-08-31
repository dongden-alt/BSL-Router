"""Shared fixtures for the BSL Router test suite.

Exists to close an isolation hole found by the 2026-08-30 capture-log audit.

The inbound capture logger keeps MODULE-GLOBAL state (`main._capture_queue`
plus the four log path constants). Two consequences made the suite unsafe:

1. **Production log pollution.** Any test that boots the FastAPI lifespan
   (`TestClient(main.app)`) spawns the real `_capture_writer_task`, and any test
   that drives the Antigravity request path enqueues real records. Because the
   writer drains through `asyncio.to_thread` *after* the test function returns,
   per-test `monkeypatch.setattr(builtins, "open", ...)` guards cannot intercept
   it — the write happens once the patch is already undone. Verified by deleting
   `.brain/logs/antigravity_inbound.jsonl`, running the suite with no live
   traffic, and watching the file come back.

2. **Order-dependent failure.** `_capture_queue` survives between tests, so
   `test_capture_line_direct_write_fallback_no_running_loop` passed in isolation
   and failed in a full-suite run, where an earlier test had left a populated
   queue in the module global.

The autouse fixture below fixes the class of bug rather than the two known
instances: no test can reach the real log directory, and no test inherits queue
state from another. Modules that need their own paths (e.g.
`test_capture_log_rotation.py`) still monkeypatch on top of this — their patch
is applied later and therefore wins.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main  # noqa: E402
import app.mitm as mitm  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_capture_logs(tmp_path, monkeypatch):
    """Redirect every capture/telemetry log into tmp_path and reset the queue.

    Autouse so it also covers tests written before this guard existed, and any
    future test that boots the lifespan without knowing the capture logger has
    global state.
    """
    log_dir = tmp_path / "capture-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main, "_CAPTURE_PATH", str(log_dir / "antigravity_inbound.jsonl"))
    monkeypatch.setattr(main, "_CAPTURE_MITM_LOG_PATH", str(log_dir / "mitm_egress_frames.jsonl"))
    monkeypatch.setattr(main, "_CAPTURE_MITM_DEBUG_LOG_PATH", str(log_dir / "mitm_live_debug.log"))
    monkeypatch.setattr(mitm, "_TELEMETRY_PATH", str(log_dir / "mitm_egress_frames.jsonl"))
    monkeypatch.setattr(mitm, "_DEBUG_LOG", str(log_dir / "mitm_live_debug.log"))

    # A queue left over from an earlier test would make queue-state assertions
    # order-dependent, so start every test from the unset default.
    monkeypatch.setattr(main, "_capture_queue", None)

    yield

    # Drop anything still queued BEFORE monkeypatch restores the real paths, so
    # a writer task draining during teardown cannot land in .brain/logs.
    # monkeypatch's own finalizer runs after this fixture's teardown, because it
    # was created earlier in the setup order.
    queue = getattr(main, "_capture_queue", None)
    if queue is not None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except Exception:
                break
    main._capture_queue = None
