"""
Tests for the OrphanBlockGuard — defensive egress layer inside
AnthropicStreamToolGuard (app/middleware/anthropic_tools.py).

THE BUG: a ``content_block_stop`` for an index whose ``content_block_start``
was never emitted reaches Claude Code, which looks the block up in its state
map, finds nothing, and aborts the whole session with
"API Error: Content block not found" (CBNF). Captured from real traffic
(GLM-5.x Anthropic-native channels): text block#0 opened and closed normally,
then an ORPHAN stop for index 1 (never opened — the indices jump 0 -> 2),
then three perfectly healthy tool_use blocks at indices 2/3/4 with
stop_reason=tool_use and a proper message_stop. ONE orphan frame destroyed
the entire run.

The guard is deliberately defensive, not causal: whether the upstream emits
the orphan or something in the router drops the matching start is unknown —
an orphan frame must never reach a client no matter who produced it.
"""
import json
import random

from app.middleware.anthropic_tools import AnthropicStreamToolGuard


# ── SSE helpers ─────────────────────────────────────────────────────────────


def _sse(event_dict, event_name=None):
    lines = []
    if event_name:
        lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(event_dict, ensure_ascii=False)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _message_start():
    return _sse(
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
        "message_start",
    )


def _message_stop(stop_reason="end_turn"):
    return [
        _sse(
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}},
            "message_delta",
        ),
        _sse({"type": "message_stop"}, "message_stop"),
    ]


def _run(frames, tools_in_request=True, chunk_size=None):
    """Feed frames through a guard, flush, return (emitted bytes, guard)."""
    guard = AnthropicStreamToolGuard(tools_in_request=tools_in_request)
    stream = b"".join(frames)
    out = []
    if chunk_size:
        for i in range(0, len(stream), chunk_size):
            out.extend(guard.feed(stream[i : i + chunk_size]))
    else:
        out.extend(guard.feed(stream))
    out.extend(guard.flush())
    return b"".join(out), guard


def _events(out_bytes):
    """Parse an emitted byte stream into (event_name, payload) tuples."""
    events = []
    for block in out_bytes.split(b"\n\n"):
        name = None
        payload = None
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                name = line[6:].strip().decode("utf-8")
            elif line.startswith(b"data:"):
                try:
                    payload = json.loads(line[5:].strip().decode("utf-8"))
                except Exception:
                    payload = None
        if name is not None or payload is not None:
            events.append((name, payload))
    return events


def _validate_content_blocks(events):
    """Local content-block state machine — the contract Claude Code enforces.

    A block opens on content_block_start and closes on content_block_stop.
    A delta or stop naming a non-open index is a violation. Stream end with
    an open block is a violation. Returns the list of violations ([] = VALID).
    """
    violations = []
    open_blocks = set()
    for _, ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        idx = ev.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            continue
        if etype == "content_block_start":
            if idx in open_blocks:
                violations.append(f"duplicate start for open index {idx}")
            open_blocks.add(idx)
        elif etype == "content_block_delta":
            if idx not in open_blocks:
                violations.append(f"delta for non-open index {idx}")
        elif etype == "content_block_stop":
            if idx not in open_blocks:
                violations.append(f"stop for non-open index {idx}")
            open_blocks.discard(idx)
    if open_blocks:
        violations.append(f"stream ended with open blocks {sorted(open_blocks)}")
    return violations


def _captured_topology_frames():
    """The EXACT captured CBNF stream: text#0, ORPHAN stop#1, tool blocks 2/3/4."""
    frames = [_message_start()]
    frames.append(
        _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            "content_block_start",
        )
    )
    for i in range(58):
        frames.append(
            _sse(
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": f"chunk{i} "}},
                "content_block_delta",
            )
        )
    frames.append(_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
    # ORPHAN — index 1 was NEVER opened; indices jump 0 -> 2.
    frames.append(_sse({"type": "content_block_stop", "index": 1}, "content_block_stop"))
    for idx in (2, 3, 4):
        frames.append(
            _sse(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "tool_use", "id": f"toolu_{idx}", "name": "Read", "input": {}},
                },
                "content_block_start",
            )
        )
        frames.append(
            _sse(
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "a.txt"}'},
                },
                "content_block_delta",
            )
        )
        frames.append(_sse({"type": "content_block_stop", "index": idx}, "content_block_stop"))
    frames.extend(_message_stop(stop_reason="tool_use"))
    return frames


def _healthy_frames():
    """Healthy stream: blocks 0 (text), 1 (thinking), 2 (tool_use) all opened
    and closed properly."""
    frames = [_message_start()]
    frames.append(
        _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            "content_block_start",
        )
    )
    frames.append(
        _sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            "content_block_delta",
        )
    )
    frames.append(_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
    frames.append(
        _sse(
            {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking", "thinking": ""}},
            "content_block_start",
        )
    )
    frames.append(
        _sse(
            {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
            "content_block_delta",
        )
    )
    frames.append(_sse({"type": "content_block_stop", "index": 1}, "content_block_stop"))
    frames.append(
        _sse(
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
            },
            "content_block_start",
        )
    )
    frames.append(
        _sse(
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "x.txt"}'},
            },
            "content_block_delta",
        )
    )
    frames.append(_sse({"type": "content_block_stop", "index": 2}, "content_block_stop"))
    frames.extend(_message_stop())
    return frames


# ── 1. the exact captured topology ──────────────────────────────────────────


def test_captured_topology_output_valid_and_no_stop_for_index_1(capsys):
    out, guard = _run(_captured_topology_frames())
    events = _events(out)
    assert _validate_content_blocks(events) == []

    # No stop for index 1 survives — the orphan was dropped.
    assert not any(
        ev and ev.get("type") == "content_block_stop" and ev.get("index") == 1
        for _, ev in events
    )
    # The three tool_use blocks survive intact, in order.
    tool_starts = [
        ev
        for _, ev in events
        if ev and ev.get("type") == "content_block_start" and ev.get("content_block", {}).get("type") == "tool_use"
    ]
    assert [s["index"] for s in tool_starts] == [2, 3, 4]
    # Their arguments survive.
    tool_deltas = [
        ev
        for _, ev in events
        if ev and ev.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert [json.loads(d["delta"]["partial_json"]) for d in tool_deltas] == [
        {"file_path": "a.txt"} for _ in range(3)
    ]
    # Text content survived byte-for-byte.
    text = "".join(
        ev["delta"]["text"]
        for _, ev in events
        if ev and ev.get("delta", {}).get("type") == "text_delta"
    )
    assert text == "".join(f"chunk{i} " for i in range(58))
    # stop_reason=tool_use preserved.
    assert any(
        ev and ev.get("delta", {}).get("stop_reason") == "tool_use" for _, ev in events
    )
    # Warning printed exactly once for index 1.
    assert (
        capsys.readouterr().out.count(
            "[OrphanBlockGuard] dropped content_block_stop for index 1"
        )
        == 1
    )
    # Internal state: only real blocks tracked; the orphan never entered it.
    assert guard._opened == {0, 2, 3, 4}
    assert guard._closed == {0, 2, 3, 4}


def test_captured_topology_chunked_feed_same_result():
    # Splitting the stream at arbitrary byte boundaries (real TCP chunks)
    # must not change the outcome.
    out_whole, _ = _run(_captured_topology_frames())
    out_chunked, _ = _run(_captured_topology_frames(), chunk_size=7)
    assert _validate_content_blocks(_events(out_chunked)) == []
    assert not any(
        ev and ev.get("type") == "content_block_stop" and ev.get("index") == 1
        for _, ev in _events(out_chunked)
    )
    assert _events(out_chunked) == _events(out_whole)


# ── 2. healthy stream is byte-identical and silent ──────────────────────────


def test_healthy_stream_byte_identical_and_silent(capsys):
    frames = _healthy_frames()
    original = b"".join(frames)
    out, guard = _run(frames)
    assert out == original  # byte-identical
    # No warnings of any kind on a healthy stream.
    assert capsys.readouterr().out == ""
    assert _validate_content_blocks(_events(out)) == []
    assert guard._opened == {0, 1, 2}
    assert guard._closed == {0, 1, 2}


def test_healthy_stream_chunked_feed_byte_identical(capsys):
    frames = _healthy_frames()
    original = b"".join(frames)
    out, _ = _run(frames, chunk_size=5)
    assert out == original
    assert capsys.readouterr().out == ""


# ── 3. orphan text_delta gets a synthetic start ─────────────────────────────


# ── 4. orphan input_json_delta is dropped ───────────────────────────────────


# ── 5. orphan stop for a thinking block index ───────────────────────────────


def test_orphan_stop_for_thinking_index_dropped(capsys):
    frames = [
        _message_start(),
        _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            "content_block_start",
        ),
        _sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "h"}},
            "content_block_delta",
        ),
        _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        # orphan stop: a thinking block at index 1 was never started
        _sse({"type": "content_block_stop", "index": 1}, "content_block_stop"),
    ]
    frames.extend(_message_stop())
    out, _ = _run(frames)
    events = _events(out)
    assert _validate_content_blocks(events) == []
    assert not any(
        ev and ev.get("type") == "content_block_stop" and ev.get("index") == 1
        for _, ev in events
    )
    # The real thinking block's content survives.
    assert any(ev and ev.get("delta", {}).get("thinking") == "h" for _, ev in events)
    assert (
        "[OrphanBlockGuard] dropped content_block_stop for index 1"
        in capsys.readouterr().out
    )


# ── 6. repeated orphan stops for the same index ──────────────────────────────


def test_repeated_orphan_stops_dropped_every_time_warn_once(capsys):
    frames = [
        _message_start(),
    ]
    for _ in range(4):
        frames.append(_sse({"type": "content_block_stop", "index": 7}, "content_block_stop"))
    frames.extend(_message_stop())
    out, _ = _run(frames)
    events = _events(out)
    assert _validate_content_blocks(events) == []
    # Dropped EVERY time.
    assert not any(
        ev and ev.get("type") == "content_block_stop" and ev.get("index") == 7
        for _, ev in events
    )
    # Warning printed exactly ONCE for the index.
    assert (
        capsys.readouterr().out.count(
            "[OrphanBlockGuard] dropped content_block_stop for index 7"
        )
        == 1
    )


# ── 7. properly opened tool_use keeps the held/repair behavior ──────────────


def test_opened_tool_use_malformed_json_still_repaired(capsys):
    # The exact GLM-5.2 signature failure (unquoted key AND unquoted value).
    frames = [
        _message_start(),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Glob", "input": {}},
            },
            "content_block_start",
        ),
        _sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{Includes: *.js}"}},
            "content_block_delta",
        ),
        _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
    ]
    frames.extend(_message_stop(stop_reason="tool_use"))
    out, guard = _run(frames)
    events = _events(out)
    assert _validate_content_blocks(events) == []
    # Repaired into ONE valid input_json_delta.
    tool_deltas = [
        ev
        for _, ev in events
        if ev and ev.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert len(tool_deltas) == 1
    assert json.loads(tool_deltas[0]["delta"]["partial_json"]) == {"Includes": "*.js"}
    captured = capsys.readouterr().out
    assert "[AnthropicToolRepair] stream repaired tool_use block 0" in captured
    # Properly opened block → NO orphan warnings.
    assert "OrphanBlockGuard" not in captured
    assert guard._opened == {0}
    assert guard._closed == {0}


def test_opened_tool_use_valid_json_still_verbatim(capsys):
    frames = [
        _message_start(),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
            },
            "content_block_start",
        ),
        _sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "a.txt"}'}},
            "content_block_delta",
        ),
        _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
    ]
    frames.extend(_message_stop(stop_reason="tool_use"))
    original = b"".join(frames)
    out, _ = _run(frames)
    assert out == original  # valid JSON → held and flushed byte-exact
    assert capsys.readouterr().out == ""


# ── 8. tools_in_request=False → byte-exact passthrough, NO suppression ─────


def test_inactive_guard_byte_exact_no_suppression_no_tracking(capsys):
    # Feed the CBNF stream itself: the inactive guard must NOT parse, drop,
    # or track anything — byte-exact passthrough, orphan included.
    frames = _captured_topology_frames()
    original = b"".join(frames)
    out, guard = _run(frames, tools_in_request=False)
    assert out == original
    # The orphan frame is STILL THERE (inactive guard performs no suppression).
    assert b'"content_block_stop", "index": 1' in out
    assert capsys.readouterr().out == ""
    # No index tracking state was populated.
    assert guard._opened == set()
    assert guard._closed == set()
    assert guard._orphan_warned == set()


def test_inactive_guard_chunked_feed_byte_exact():
    frames = _captured_topology_frames()
    original = b"".join(frames)
    out, _ = _run(frames, tools_in_request=False, chunk_size=11)
    assert out == original


# ── 9. fail-open: garbage never raises ──────────────────────────────────────


def test_malformed_and_garbage_frames_never_raise():
    nasties = [
        b"",
        b"event: content_block_stop\r\ndata: not json\r\n\r\n",
        b"data: {\"type\": \"content_block_stop\", \"index\": \"not-an-int\"}\n\n",
        b"data: [1, 2, 3]\n\n",  # JSON but not a dict
        b"data: {\"type\": \"content_block_delta\", \"index\": null, \"delta\": {\"type\": \"text_delta\", \"text\": \"x\"}}\n\n",
        b"event: content_block_start\ndata: {\"type\": \"content_block_start\", \"index\": true, \"content_block\": {\"type\": \"text\", \"text\": \"\"}}\n\n",
        b"\xff\xfe binary garbage \x00\x01\n\n",
        # orphan stop with no prior start (exercises the guard itself)
        b"event: content_block_stop\ndata: {\"type\": \"content_block_stop\", \"index\": 0}\n\n",
        # orphan delta with no prior start
        b"event: content_block_delta\ndata: {\"type\": \"content_block_delta\", \"index\": 1, \"delta\": {\"type\": \"text_delta\", \"text\": \"t\"}}\n\n",
    ]
    guard = AnthropicStreamToolGuard(tools_in_request=True)
    for chunk in nasties:
        out = guard.feed(chunk)  # must not raise
        assert isinstance(out, list)
        assert all(isinstance(b, bytes) for b in out)
    out = guard.flush()
    assert isinstance(out, list)
    assert all(isinstance(b, bytes) for b in out)


def test_captured_stream_split_at_random_offsets_never_raises():
    blob = b"".join(_captured_topology_frames()) + b"\x00\xff partial-event-without-terminator"
    guard = AnthropicStreamToolGuard(tools_in_request=True)
    random.seed(1234)
    pos = 0
    collected = []
    while pos < len(blob):
        n = random.randint(1, 17)
        collected.extend(guard.feed(blob[pos : pos + n]))  # must not raise
        pos += n
    collected.extend(guard.flush())
    joined = b"".join(collected)
    # The stream must still terminate properly — message_stop survives.
    assert b'"message_stop"' in joined
    # And the orphan never leaked despite arbitrary chunk boundaries.
    assert b'"content_block_stop", "index": 1' not in joined


# ── 10. interleaved blocks: only the orphan is suppressed ───────────────────


def test_interleaved_only_orphan_suppressed_others_intact(capsys):
    frames = [
        _message_start(),
        _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            "content_block_start",
        ),
        # orphan stop for index 1 interleaved BEFORE block 2 starts
        _sse({"type": "content_block_stop", "index": 1}, "content_block_stop"),
        _sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "A"}},
            "content_block_delta",
        ),
        _sse(
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            "content_block_start",
        ),
        _sse(
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "B"}},
            "content_block_delta",
        ),
        _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        _sse({"type": "content_block_stop", "index": 2}, "content_block_stop"),
    ]
    frames.extend(_message_stop())
    out, guard = _run(frames)
    events = _events(out)
    assert _validate_content_blocks(events) == []
    # Blocks 0 and 2 pass through INTACT and in order; only the orphan died.
    seq = [
        (ev["type"], ev.get("index"))
        for _, ev in events
        if ev and str(ev.get("type", "")).startswith("content_block")
    ]
    assert seq == [
        ("content_block_start", 0),
        ("content_block_delta", 0),
        ("content_block_start", 2),
        ("content_block_delta", 2),
        ("content_block_stop", 0),
        ("content_block_stop", 2),
    ]
    assert not any(
        ev and ev.get("type") == "content_block_stop" and ev.get("index") == 1
        for _, ev in events
    )
    assert guard._opened == {0, 2}
    assert guard._closed == {0, 2}
    assert (
        "[OrphanBlockGuard] dropped content_block_stop for index 1"
        in capsys.readouterr().out
    )
