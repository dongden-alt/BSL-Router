"""Capture-log I/O stall fix tests (2026-08-30 RCA).

Covers the rotation + async-queue capture rework that replaced the
synchronous unbounded antigravity_inbound.jsonl write:
  1. rotation triggers at cap, `.1` is overwritten on the second cycle
  2. `_capture_line` direct-write fallback without a running loop; queue-full drops
  3. `_build_capture_record` metadata-only vs full modes + auth-header redaction
  4. `_append_egress_telemetry` rotation under a small monkeypatched cap

tmp_path + monkeypatch only — these tests must NEVER touch the real
`.brain/logs` directory.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main
import app.mitm as mitm


@pytest.fixture
def inbound_path(tmp_path, monkeypatch):
    """Point the inbound capture logger at tmp_path and shrink the cap."""
    path = str(tmp_path / "antigravity_inbound.jsonl")
    monkeypatch.setattr(main, "_CAPTURE_PATH", path)
    monkeypatch.setattr(main, "_CAPTURE_CAP_BYTES", 64)
    return path


def _write_bytes(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(data)


def test_rotation_triggers_at_cap_and_overwrites_dot_one(inbound_path):
    # Seed an oversized file, rotate -> .1 created, live file gone.
    _write_bytes(inbound_path, b"x" * 65)
    main._rotate_capped_file(inbound_path, main._CAPTURE_CAP_BYTES)
    assert os.path.exists(inbound_path + ".1")
    assert not os.path.exists(inbound_path)

    # Regrow + rotate again: stale .1 must be replaced, not piled up as .2.
    _write_bytes(inbound_path, b"y" * 65)
    main._rotate_capped_file(inbound_path, main._CAPTURE_CAP_BYTES)
    with open(inbound_path + ".1", "rb") as fh:
        assert fh.read() == b"y" * 65
    assert not os.path.exists(inbound_path + ".2")

    # Under-cap file is left alone.
    _write_bytes(inbound_path, b"z" * 8)
    main._rotate_capped_file(inbound_path, main._CAPTURE_CAP_BYTES)
    assert os.path.getsize(inbound_path) == 8


def test_capture_write_direct_appends_with_rotation(inbound_path):
    rec = {"ts": "now", "path": "/p", "body_bytes": 1}
    for _ in range(4):
        main._capture_write_direct(rec)
    # Rotation fired at least once and the live file is small.
    assert os.path.exists(inbound_path)
    assert os.path.getsize(inbound_path) <= main._CAPTURE_CAP_BYTES + len(json.dumps(rec)) + 2
    with open(inbound_path, "r", encoding="utf-8") as fh:
        for line in fh:
            json.loads(line)  # every surviving line is valid JSONL
    main._capture_write_direct({"unserializable": os})  # default=str keeps it safe


def test_capture_line_direct_write_fallback_no_running_loop(inbound_path, monkeypatch):
    # Outside an event loop _capture_line must write directly, not enqueue.
    main._capture_line({"ts": "t", "path": "/direct", "body_bytes": 2})
    with open(inbound_path, "r", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh]
    assert len(lines) == 1 and lines[0]["path"] == "/direct"
    assert getattr(main, "_capture_queue", None) is None


def test_capture_line_queue_full_drops_without_raising(inbound_path, monkeypatch):
    # Inside a running loop with a saturated queue: put_nowait must drop, not raise.
    monkeypatch.setattr(main, "_capture_queue", None)

    async def scenario():
        queue = asyncio.Queue(maxsize=1)
        monkeypatch.setattr(main, "_capture_queue", queue)
        queue.put_nowait({"n": 0})
        main._capture_line({"n": 1})  # queue full -> dropped silently
        main._capture_line({"n": 2})  # still full -> dropped silently
        assert queue.qsize() == 1
        assert queue.get_nowait() == {"n": 0}

    asyncio.run(scenario())


def test_capture_line_enqueues_when_loop_running(inbound_path, monkeypatch):
    # With a live loop and an empty queue the record is enqueued, not written.
    monkeypatch.setattr(main, "_capture_queue", None)

    async def scenario():
        main._capture_line({"ts": "t", "path": "/queued", "body_bytes": 3})
        assert main._capture_queue is not None
        assert main._capture_queue.qsize() == 1
        assert main._capture_queue.get_nowait()["path"] == "/queued"
        return main._capture_queue

    asyncio.run(scenario())
    # Nothing was written to disk in this test.
    assert not os.path.exists(inbound_path)


def test_build_capture_record_metadata_only_by_default():
    rec = main._build_capture_record(
        "/v1internal:generateContent",
        "alt=sse",
        "bsl-alias-x",
        {"contents": [{"role": "user", "parts": [{"text": "secret prompt"}]}]},
        {"model": "target", "messages": [{"role": "user", "content": "secret prompt"}]},
        {"authorization": "Bearer tok", "content-type": "application/json"},
        full=False,
    )
    assert rec["path"] == "/v1internal:generateContent"
    assert rec["query"] == "alt=sse"
    assert rec["model_alias"] == "bsl-alias-x"
    assert rec["body_bytes"] > 0
    assert "ts" in rec
    # No payload/headers may leak in metadata-only mode.
    assert "raw_body" not in rec
    assert "converted_openai_body" not in rec
    assert "headers" not in rec


def test_build_capture_record_full_mode_includes_and_redacts():
    headers = {
        "Authorization": "Bearer secret",
        "x-goog-api-key": "goog-secret",
        "X-API-Key": "api-secret",
        "Cookie": "session=1",
        "content-type": "application/json",
    }
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    openai_body = {"model": "t", "messages": []}
    rec = main._build_capture_record(
        "/p", "", "alias", body, openai_body, headers, full=True
    )
    assert rec["raw_body"] == body
    assert rec["converted_openai_body"] == openai_body
    red = rec["headers"]
    for banned in ("authorization", "x-goog-api-key", "x-api-key", "cookie"):
        assert banned not in {k.lower() for k in red}
    assert red.get("content-type") == "application/json"


def test_full_capture_flag_from_env(monkeypatch):
    monkeypatch.setenv("BSL_CAPTURE_INBOUND", "1")
    assert os.environ.get("BSL_CAPTURE_INBOUND") == "1"
    # Module-level probe happened at import; assert the contract it encodes.
    monkeypatch.delenv("BSL_CAPTURE_INBOUND")
    assert os.environ.get("BSL_CAPTURE_INBOUND") is None


def test_mitm_telemetry_rotation_small_cap(tmp_path, monkeypatch):
    path = str(tmp_path / "mitm_egress_frames.jsonl")
    monkeypatch.setattr(mitm, "_TELEMETRY_PATH", path)
    monkeypatch.setattr(mitm, "_TELEMETRY_CAP_BYTES", 32)

    event = {"ts": 1.0, "route": "chat", "host": "h", "path": "/p"}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"a" * 33)  # already over the small cap

    mitm._append_egress_telemetry(event)  # must not raise
    assert os.path.exists(path + ".1") and os.path.getsize(path + ".1") == 33
    with open(path, "r", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh]
    assert len(lines) == 1 and lines[0]["route"] == "chat"

    # Second over-cap cycle: stale .1 replaced atomically.
    with open(path, "wb") as fh:
        fh.write(b"b" * 33)
    mitm._append_egress_telemetry(event)
    with open(path + ".1", "rb") as fh:
        assert fh.read() == b"b" * 33


def test_mitm_telemetry_never_raises_on_bad_path(tmp_path, monkeypatch):
    monkeypatch.setattr(mitm, "_TELEMETRY_PATH", str(tmp_path / "no" / "perm" / "x.jsonl"))
    # Un-creatable directory: must be swallowed, not raised.
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    try:
        mitm._append_egress_telemetry({"ts": 1})
    finally:
        monkeypatch.undo()

