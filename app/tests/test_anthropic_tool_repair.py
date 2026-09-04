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
