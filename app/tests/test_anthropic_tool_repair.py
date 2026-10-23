"""
Tests for app/middleware/anthropic_tools.py — same-dialect (Anthropic→Anthropic)
tool-call hygiene for the Claude Code passthrough path.

Binding note: unit tests bind to the REAL middleware module; integration tests
bind to the REAL app.main source (wiring characterization — the house precedent
for asserting integration that only activates on live traffic, per the launcher
tests and test_sse_accumulator_integration.py).
"""
import inspect
import json

from app.middleware.anthropic_tools import (
    AnthropicStreamLifecycleTracker,
    AnthropicStreamToolGuard,
    anthropic_tool_repair_enabled,
    repair_anthropic_response_tool_uses,
    repair_tool_input_json,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _sse(event_dict, event_name=None):
    lines = []
    if event_name:
        lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(event_dict, ensure_ascii=False)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _tool_stream(partial_jsons):
    """Minimal Anthropic SSE stream with one tool_use block at index 0."""
    out = [
        _sse({"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 1}}}, "message_start"),
        _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}}, "content_block_start"),
    ]
    for pj in partial_jsons:
        out.append(_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": pj}}, "content_block_delta"))
    out.append(_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
    out.append(_sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}, "message_delta"))
    out.append(_sse({"type": "message_stop"}, "message_stop"))
    return b"".join(out)


# ── repair_tool_input_json ──────────────────────────────────────────────────


class TestRepairToolInputJson:
    def test_valid_json_identity(self):
        s = '{"path": "a.txt", "n": 5}'
        assert repair_tool_input_json(s) == s

    def test_unquoted_value(self):
        # The exact GLM-5.2 failure observed in child_out.log.
        assert json.loads(repair_tool_input_json('{"Includes": *.js}')) == {"Includes": "*.js"}

    def test_unquoted_key(self):
        assert json.loads(repair_tool_input_json('{Includes: "*.js"}')) == {"Includes": "*.js"}

    def test_key_and_value(self):
        assert json.loads(repair_tool_input_json('{Includes: *.js}')) == {"Includes": "*.js"}

    def test_truncated_object(self):
        assert json.loads(repair_tool_input_json('{"a": "b"')) == {"a": "b"}

    def test_unterminated_string(self):
        assert json.loads(repair_tool_input_json('{"a": "b')) == {"a": "b"}

    def test_trailing_backslash(self):
        out = repair_tool_input_json('{"a": "b\\')
        assert out is not None and json.loads(out) == {"a": "b\\"}

    def test_nested(self):
        raw = '{"glob": {"Includes": *.js}, "n": 1}'
        assert json.loads(repair_tool_input_json(raw)) == {"glob": {"Includes": "*.js"}, "n": 1}

    def test_valid_scalars_untouched(self):
        s = '{"n": 5, "b": true, "x": null, "f": -1.5e3, "z": 0}'
        assert repair_tool_input_json(s) == s

    def test_array_values_untouched(self):
        s = '{"exts": [".js", ".ts"]}'
        assert repair_tool_input_json(s) == s

    def test_garbage_returns_none(self):
        assert repair_tool_input_json("not json at all {{{") is None

    def test_empty_returns_none(self):
        assert repair_tool_input_json("") is None
        assert repair_tool_input_json("   ") is None


# ── AnthropicStreamToolGuard ────────────────────────────────────────────────


class TestAnthropicStreamToolGuard:
    def test_valid_args_byte_identical(self):
        stream = _tool_stream(['{"pa', 'th": "a.txt"}'])
        g = AnthropicStreamToolGuard(tools_in_request=True)
        out = []
        # Awkward 7-byte slices exercise chunk-boundary framing.
        for i in range(0, len(stream), 7):
            out.extend(g.feed(stream[i:i + 7]))
        out.extend(g.flush())
        assert b"".join(out) == stream

    def test_malformed_args_repaired(self):
        stream = _tool_stream(['{"Includes": ', '*.js}'])
        g = AnthropicStreamToolGuard(tools_in_request=True)
        out = list(g.feed(stream)) + g.flush()
        joined = b"".join(out)
        # json.dumps re-escapes the replacement delta's quotes: the repaired
        # fragment appears as \"*.js\" inside the data: line.
        assert b'\\"*.js\\"' in joined
        assert b'*.js' in joined
        # Framing intact: exactly one stop event, terminal frames survive.
        assert joined.count(b'"content_block_stop"') == 1
        assert b'"message_stop"' in joined

    def test_repaired_stream_parses_end_to_end(self):
        """The repaired output must reassemble to valid tool input."""
        stream = _tool_stream(['{"Includes": ', '*.js}'])
        g = AnthropicStreamToolGuard(tools_in_request=True)
        joined = b"".join(list(g.feed(stream)) + g.flush())
        fragments = []
        for line in joined.split(b"\n"):
            if line.startswith(b"data:"):
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                d = ev.get("delta") or {}
                if d.get("type") == "input_json_delta":
                    fragments.append(d.get("partial_json", ""))
        assert json.loads("".join(fragments)) == {"Includes": "*.js"}

    def test_eof_held_block_flushed_verbatim(self):
        part = (
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "Read", "input": {}}}, "content_block_start")
            + _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a": '}}, "content_block_delta")
        )
        g = AnthropicStreamToolGuard(tools_in_request=True)
        out = list(g.feed(part)) + g.flush()
        assert b"".join(out) == part  # never swallow bytes

    def test_no_tools_identity(self):
        stream = _tool_stream(['{"a": 1}'])
        g = AnthropicStreamToolGuard(tools_in_request=False)
        out = list(g.feed(stream)) + g.flush()
        assert b"".join(out) == stream

    def test_cap_flush_passthrough(self):
        part_start = _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "Read", "input": {}}}, "content_block_start")
        big = "x" * 300
        delta = _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": big}}, "content_block_delta")
        stop = _sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
        g = AnthropicStreamToolGuard(tools_in_request=True, max_block_bytes=256)
        out = list(g.feed(part_start + delta)) + list(g.feed(stop)) + g.flush()
        assert b"".join(out) == part_start + delta + stop  # verbatim

    def test_text_delta_passes_while_tool_block_held(self):
        held_start = _sse({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t", "name": "Read", "input": {}}}, "content_block_start")
        text_evt = _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}, "content_block_delta")
        g = AnthropicStreamToolGuard(tools_in_request=True)
        out = list(g.feed(held_start + text_evt))
        assert out == [text_evt]  # start held; text passed immediately


# ── Non-stream repair ───────────────────────────────────────────────────────


class TestRepairAnthropicResponseToolUses:
    def test_str_input_repaired(self):
        resp = {"content": [{"type": "tool_use", "id": "t", "name": "Glob", "input": '{"Includes": *.js}'}]}
        out, mut = repair_anthropic_response_tool_uses(resp)
        assert mut is True
        assert out["content"][0]["input"] == {"Includes": "*.js"}

    def test_valid_str_parsed(self):
        resp = {"content": [{"type": "tool_use", "id": "t", "name": "Glob", "input": '{"a": 1}'}]}
        out, mut = repair_anthropic_response_tool_uses(resp)
        assert mut is True
        assert out["content"][0]["input"] == {"a": 1}

    def test_garbage_str_untouched(self):
        resp = {"content": [{"type": "tool_use", "id": "t", "name": "Glob", "input": "{{{garbage"}]}
        out, mut = repair_anthropic_response_tool_uses(resp)
        assert mut is False
        assert out["content"][0]["input"] == "{{{garbage"

    def test_dict_untouched(self):
        resp = {"content": [{"type": "tool_use", "id": "t", "name": "Glob", "input": {"a": 1}}]}
        out, mut = repair_anthropic_response_tool_uses(resp)
        assert mut is False
        assert out["content"][0]["input"] == {"a": 1}

    def test_no_tool_use_identity(self):
        resp = {"content": [{"type": "text", "text": "hi"}]}
        out, mut = repair_anthropic_response_tool_uses(resp)
        assert mut is False
        assert out == resp


# ── Config gate ─────────────────────────────────────────────────────────────


class TestConfigGate:
    def test_default_on(self):
        assert anthropic_tool_repair_enabled({}) is True
        assert anthropic_tool_repair_enabled(None) is True
        assert anthropic_tool_repair_enabled({"tools": {}}) is True

    def test_kill_switch(self):
        assert anthropic_tool_repair_enabled({"tools": {"anthropic_tool_repair": False}}) is False


# ── main.py wiring characterization ─────────────────────────────────────────


class TestMainIntegration:
    """The guard must be wired into BOTH real passthrough sites in app/main.py
    (streaming raw_upstream loop + non-stream passthrough return), and the
    stream-buffer OpenAI-shape fix must exist for Anthropic clients."""

    def _main_src(self):
        import app.main as m
        return inspect.getsource(m)

    def test_import_wired(self):
        src = self._main_src()
        assert "from app.middleware.anthropic_tools import" in src

    def test_stream_site_wired(self):
        src = self._main_src()
        assert "AnthropicStreamToolGuard(tools_in_request=True)" in src
        assert "_tool_guard.feed(chunk)" in src
        assert "_tool_guard.flush()" in src
        # Gate must include the anthropic×anthropic-fmt×tools conditions.
        assert "client_wants_anthropic and _is_anthropic_fmt" in src

    def test_nonstream_site_wired(self):
        src = self._main_src()
        assert "repair_anthropic_response_tool_uses(_passthrough_json)" in src

    def test_stream_buffer_shape_fix_wired(self):
        src = self._main_src()
        assert "STREAM-BUFFER SHAPE FIX" in src
        assert "openai_response_to_anthropic(_sbx_openai" in src

    def test_stream_lifecycle_tail_synthesis_wired(self):
        src = self._main_src()
        assert "AnthropicStreamLifecycleTracker()" in src
        assert "_anth_lifecycle.observe_event(" in src
        assert "_anth_lifecycle.synthesize_missing_tail(" in src


# ── AnthropicStreamLifecycleTracker (missing terminal frames at EOF) ─────────


class TestAnthropicStreamLifecycleTracker:
    def test_healthy_stream_emits_nothing(self):
        t = AnthropicStreamLifecycleTracker()
        t.observe_event({"type": "message_start", "message": {"id": "m1"}})
        t.observe_event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        t.observe_event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}})
        t.observe_event({"type": "content_block_stop", "index": 0})
        t.observe_event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
        t.observe_event({"type": "message_stop"})
        assert t.synthesize_missing_tail(output_tokens=5) == []

    def test_agentrouter_gpt_6_1_sol_thinking_plus_unclosed_text(self):
        """Exact agentrouter/gpt-6.1-sol stream shape: index 0 thinking closed,
        index 1 text opened + delta emitted, then EOF without content_block_stop(1),
        message_delta, or message_stop."""
        t = AnthropicStreamLifecycleTracker()
        t.observe_event({"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 41483, "output_tokens": 0}}})
        t.observe_event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
        t.observe_event({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}})
        t.observe_event({"type": "content_block_stop", "index": 0})
        t.observe_event({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
        t.observe_event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Answer text"}})

        tail = t.synthesize_missing_tail(output_tokens=96)
        assert len(tail) == 3
        ev0 = json.loads(tail[0].decode("utf-8").split("data: ", 1)[1])
        ev1 = json.loads(tail[1].decode("utf-8").split("data: ", 1)[1])
        ev2 = json.loads(tail[2].decode("utf-8").split("data: ", 1)[1])
        assert ev0 == {"type": "content_block_stop", "index": 1}
        assert ev1 == {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 96},
        }
        assert ev2 == {"type": "message_stop"}
        # Idempotent if called again
        assert t.synthesize_missing_tail(output_tokens=96) == []

    def test_agentrouter_single_text_block_unclosed(self):
        t = AnthropicStreamLifecycleTracker()
        t.observe_event({"type": "message_start", "message": {"id": "m1"}})
        t.observe_event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        t.observe_event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}})

        tail = t.synthesize_missing_tail(output_tokens=8)
        assert len(tail) == 3
        ev0 = json.loads(tail[0].decode("utf-8").split("data: ", 1)[1])
        ev1 = json.loads(tail[1].decode("utf-8").split("data: ", 1)[1])
        ev2 = json.loads(tail[2].decode("utf-8").split("data: ", 1)[1])
        assert ev0 == {"type": "content_block_stop", "index": 0}
        assert ev1["delta"]["stop_reason"] == "end_turn"
        assert ev1["usage"]["output_tokens"] == 8
        assert ev2 == {"type": "message_stop"}

    def test_tool_use_block_unclosed_sets_tool_use_stop_reason(self):
        t = AnthropicStreamLifecycleTracker()
        t.observe_event({"type": "message_start", "message": {"id": "m1"}})
        t.observe_event({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
        })
        tail = t.synthesize_missing_tail(output_tokens=25)
        assert len(tail) == 3
        ev1 = json.loads(tail[1].decode("utf-8").split("data: ", 1)[1])
        assert ev1["delta"]["stop_reason"] == "tool_use"
        assert ev1["usage"]["output_tokens"] == 25

    def test_error_stream_emits_nothing(self):
        t = AnthropicStreamLifecycleTracker()
        t.observe_event({"type": "message_start", "message": {"id": "m1"}})
        t.observe_event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        t.observe_event({"type": "error", "error": {"message": "boom"}})
        assert t.synthesize_missing_tail(output_tokens=10) == []

    def test_empty_stream_emits_nothing(self):
        t = AnthropicStreamLifecycleTracker()
        assert t.synthesize_missing_tail(output_tokens=0) == []


def test_anthropic_passthrough_synthesizes_missing_tail_and_avoids_false_degenerate(monkeypatch):
    """Call-path regression for agentrouter/gpt-6.1-sol on Anthropic->Anthropic
    streaming passthrough:
      1. Upstream emits message_start (input_tokens=45000, output_tokens=0),
         thinking block 0 (closed), text block 1 (>1000 chars, left OPEN at EOF
         without content_block_stop(1), message_delta, or message_stop).
      2. Client stream receives synthesized content_block_stop(1),
         message_delta(stop_reason='end_turn', usage.output_tokens>0), and
         message_stop.
      3. Backfilled out_tokens > 200 runs BEFORE _is_degenerate_output, so
         stats['degenerate'] is False despite in_tokens=45000 and upstream
         omitting usage.output_tokens."""
    import asyncio
    import httpx
    from starlette.requests import Request
    import app.config_state as cs
    import app.main as main

    long_answer = "Detailed architectural verification output. " * 30  # ~1320 chars -> >300 tokens
    upstream_chunks = [
        _sse(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_ar_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "gpt-6.1-sol",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 45000, "output_tokens": 0},
                },
            },
            "message_start",
        ),
        _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            "content_block_start",
        ),
        _sse(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Analyzing..."}},
            "content_block_delta",
        ),
        _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        _sse(
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            "content_block_start",
        ),
        _sse(
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": long_answer}},
            "content_block_delta",
        ),
        # Deliberately NO content_block_stop(1), NO message_delta, NO message_stop!
    ]

    class _RawStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for c in upstream_chunks:
                yield c

        async def aclose(self):
            pass

    class _FakeClient:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url, **kwargs)

        async def send(self, request, stream=False):
            return httpx.Response(200, request=request, stream=_RawStream())

    log_calls = []
    monkeypatch.setattr(main.obs, "log_request", lambda *a, **kw: log_calls.append(kw))
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kw: "req-ar-eof")
    monkeypatch.setattr(main, "_capture_queue", None)
    cs.replace_config({
        "providers": {
            "agentrouter": {
                "format": "anthropic",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://fake.invalid"}],
                "models": [{"id": "gpt-6.1-sol", "enabled": True}],
            }
        },
        "aliases": {},
        "combos": [],
        "tools": {},
        "settings": {"combo_infinite_retry": False},
    })
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: _FakeClient())

    req_body = {
        "model": "agentrouter/gpt-6.1-sol",
        "max_tokens": 4096,
        "stream": True,
        "messages": [{"role": "user", "content": "Review the architecture."}],
    }
    encoded = json.dumps(req_body).encode("utf-8")
    delivered = False

    async def _receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        _receive,
    )

    collected = []

    async def _run():
        resp = await main.anthropic_messages(req)
        async for chunk in resp.body_iterator:
            collected.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    asyncio.run(_run())
    full_sse = b"".join(collected).decode("utf-8", errors="replace")

    events = []
    for line in full_sse.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].lstrip()))
            except Exception:
                pass

    stop_indices = [e.get("index") for e in events if e.get("type") == "content_block_stop"]
    assert stop_indices == [0, 1], f"Expected content_block_stop for [0, 1], got {stop_indices}"

    msg_deltas = [e for e in events if e.get("type") == "message_delta"]
    assert len(msg_deltas) == 1
    assert msg_deltas[0]["delta"]["stop_reason"] == "end_turn"
    assert msg_deltas[0]["usage"]["output_tokens"] > 200

    msg_stops = [e for e in events if e.get("type") == "message_stop"]
    assert len(msg_stops) == 1

    end_logs = [c for c in log_calls if c.get("event", "end") == "end"]
    assert end_logs, f"Expected END log_request call, got {log_calls}"
    end_log = end_logs[-1]
    assert end_log.get("out_tokens", 0) > 200
    assert not end_log.get("degenerate", False), f"Stream with backfilled out_tokens>200 must not be flagged degenerate: {end_log}"

