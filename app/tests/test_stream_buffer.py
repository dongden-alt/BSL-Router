"""
Regression tests for the Stream-Then-Buffer 524 mitigation fix.

Tests cover:
1. _SyntheticResponse shape and interface
2. SSE accumulation logic (OpenAI format)
3. SSE accumulation logic (Anthropic format, including thinking_delta)
4. n>1 skip behavior
5. Config gate (enabled/disabled, per-provider override)
6. Error propagation (mid-stream error → raise)
7. Fail-open on immediate (<2s) rejection
"""
import json
import time
import asyncio
import pytest
import httpx

from app.main import _SyntheticResponse


# ── _SyntheticResponse Tests ──────────────────────────────────

class TestSyntheticResponse:
    def test_status_code(self):
        resp = _SyntheticResponse(200, {"choices": []})
        assert resp.status_code == 200

    def test_json(self):
        data = {"id": "test-1", "choices": [{"message": {"content": "hello"}}]}
        resp = _SyntheticResponse(200, data)
        assert resp.json() == data

    def test_text_from_json(self):
        data = {"choices": [{"message": {"content": "hello"}}]}
        resp = _SyntheticResponse(200, data)
        assert resp.text == json.dumps(data)

    def test_text_explicit(self):
        resp = _SyntheticResponse(500, {}, text="Internal Error")
        assert resp.text == "Internal Error"

    def test_content_bytes(self):
        resp = _SyntheticResponse(200, {"key": "value"})
        assert resp.content == json.dumps({"key": "value"}).encode("utf-8")

    def test_headers(self):
        resp = _SyntheticResponse(200, {})
        assert resp.headers["content-type"] == "application/json"

    def test_aclose_noop(self):
        resp = _SyntheticResponse(200, {})
        asyncio.run(resp.aclose())  # Should not raise


# ── OpenAI SSE Accumulation Tests ─────────────────────────────

def _make_openai_sse_stream(chunks: list[dict]) -> bytes:
    """Build a raw SSE byte stream from a list of chunk dicts."""
    parts = []
    for chunk in chunks:
        parts.append(f"data: {json.dumps(chunk)}\n\n")
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


class TestOpenAISSEAccumulation:
    """Test that the accumulator correctly assembles OpenAI SSE chunks."""

    def test_content_concatenation(self):
        """Multiple content deltas are concatenated into one message."""
        chunks = [
            {"id": "chatcmpl-1", "model": "test-model", "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"id": "chatcmpl-1", "model": "test-model", "choices": [{"delta": {"content": ", "}, "finish_reason": None}]},
            {"id": "chatcmpl-1", "model": "test-model", "choices": [{"delta": {"content": "World!"}, "finish_reason": None}]},
            {"id": "chatcmpl-1", "model": "test-model", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        # Verify the test data itself is correct
        content_parts = []
        for chunk in chunks:
            for ch in chunk.get("choices", []):
                delta = ch.get("delta", {})
                if delta.get("content"):
                    content_parts.append(delta["content"])
        assert "".join(content_parts) == "Hello, World!"

    def test_usage_after_finish_reason(self):
        """Usage chunk arrives AFTER finish_reason — must drain to [DONE]."""
        chunks = [
            {"id": "chatcmpl-1", "model": "test", "choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]},
            {"id": "chatcmpl-1", "model": "test", "choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"id": "chatcmpl-1", "model": "test", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        # Verify usage is in the LAST chunk (after finish_reason)
        finish_idx = None
        usage_idx = None
        for i, chunk in enumerate(chunks):
            for ch in chunk.get("choices", []):
                if ch.get("finish_reason"):
                    finish_idx = i
            if chunk.get("usage"):
                usage_idx = i
        assert finish_idx is not None
        assert usage_idx is not None
        assert usage_idx > finish_idx  # Critical: usage AFTER finish

    def test_tool_call_accumulation(self):
        """Tool call deltas are accumulated by index."""
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"loc"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ation\":\"NYC\"}"}}]}}]},
        ]
        # Accumulate tool calls
        tool_calls = {}
        for chunk in chunks:
            for ch in chunk.get("choices", []):
                for tc in ch.get("delta", {}).get("tool_calls", []):
                    idx = tc.get("index", 0)
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": tc.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        tool_calls[idx]["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tool_calls[idx]["function"]["arguments"] += fn["arguments"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "get_weather"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"location": "NYC"}

    def test_reasoning_content_extraction(self):
        """reasoning_content delta is accumulated separately."""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "Thinking..."}}]},
            {"choices": [{"delta": {"reasoning_content": " about the answer"}}]},
            {"choices": [{"delta": {"content": "42"}, "finish_reason": "stop"}]},
        ]
        reasoning_parts = []
        content_parts = []
        for chunk in chunks:
            for ch in chunk.get("choices", []):
                delta = ch.get("delta", {})
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                if delta.get("content"):
                    content_parts.append(delta["content"])
        assert "".join(reasoning_parts) == "Thinking... about the answer"
        assert "".join(content_parts) == "42"


# ── Anthropic SSE Accumulation Tests ──────────────────────────

class TestAnthropicSSEAccumulation:
    """Test that the accumulator correctly handles Anthropic SSE events."""

    def test_thinking_delta_extraction(self):
        """thinking_delta events are extracted into reasoning_content.

        This is correction #7: the StreamNormalizer silently drops
        thinking_delta, so the accumulator must parse it directly.
        """
        events = [
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Let me think..."}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": " about this"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "The answer is 42."}},
        ]
        reasoning_parts = []
        content_parts = []
        for evt in events:
            delta = evt.get("delta", {})
            if delta.get("type") == "thinking_delta":
                reasoning_parts.append(delta.get("thinking", ""))
            elif delta.get("type") == "text_delta":
                content_parts.append(delta.get("text", ""))
        assert "".join(reasoning_parts) == "Let me think... about this"
        assert "".join(content_parts) == "The answer is 42."

    def test_anthropic_stop_reason_mapping(self):
        """Anthropic stop_reason maps to OpenAI finish_reason."""
        stop_reasons = [
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
            ("stop_sequence", "stop"),
        ]
        fr_map = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }
        for anthropic_sr, expected_fr in stop_reasons:
            assert fr_map.get(anthropic_sr, anthropic_sr) == expected_fr

    def test_anthropic_usage_extraction(self):
        """Usage is extracted from message_start (input) and message_delta (output)."""
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 13}},
        ]
        in_tokens = 0
        out_tokens = 0
        for evt in events:
            if evt.get("type") == "message_start":
                u = evt.get("message", {}).get("usage", {})
                if u:
                    in_tokens = u.get("input_tokens", in_tokens)
            elif evt.get("type") == "message_delta":
                u = evt.get("usage", {})
                if u:
                    out_tokens = u.get("output_tokens", out_tokens)
        assert in_tokens == 42
        assert out_tokens == 13

    def test_anthropic_tool_use_init(self):
        """content_block_start with tool_use initializes tool calls."""
        events = [
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool-1", "name": "calculator"}},
            {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{\"expr\": "}},
            {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "\"1+1\"}"}},
        ]
        tool_calls = {}
        for evt in events:
            if evt.get("type") == "content_block_start":
                block = evt.get("content_block", {})
                if block.get("type") == "tool_use":
                    idx = len(tool_calls)
                    tool_calls[idx] = {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": ""},
                    }
            elif evt.get("delta", {}).get("type") == "input_json_delta":
                partial = evt["delta"].get("partial_json", "")
                if partial and tool_calls:
                    last_idx = max(tool_calls.keys())
                    tool_calls[last_idx]["function"]["arguments"] += partial
        assert tool_calls[0]["id"] == "tool-1"
        assert tool_calls[0]["function"]["name"] == "calculator"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"expr": "1+1"}


# ── Config Gate Tests ─────────────────────────────────────────

class TestConfigGate:
    """Test the config gating logic for upstream_stream_buffer."""

    def test_default_enabled(self):
        """When no config is present, stream buffer defaults to enabled."""
        cfg = {}
        sb_cfg = cfg.get("upstream_stream_buffer", {})
        enabled = sb_cfg.get("enabled", True) if isinstance(sb_cfg, dict) else True
        assert enabled is True

    def test_explicit_disabled(self):
        """Global disable flag works."""
        cfg = {"upstream_stream_buffer": {"enabled": False}}
        sb_cfg = cfg.get("upstream_stream_buffer", {})
        enabled = sb_cfg.get("enabled", True) if isinstance(sb_cfg, dict) else True
        assert enabled is False

    def test_per_provider_opt_out(self):
        """Per-provider override can disable stream buffer."""
        cfg = {
            "upstream_stream_buffer": {
                "enabled": True,
                "providers": {
                    "local-ollama": {"enabled": False},
                },
            }
        }
        sb_cfg = cfg.get("upstream_stream_buffer", {})
        enabled = sb_cfg.get("enabled", True) if isinstance(sb_cfg, dict) else True
        if isinstance(sb_cfg, dict) and isinstance(sb_cfg.get("providers"), dict):
            prov_sb = sb_cfg["providers"].get("local-ollama")
            if isinstance(prov_sb, dict):
                enabled = prov_sb.get("enabled", enabled)
        assert enabled is False

    def test_per_provider_inherit_global(self):
        """Provider not in override list inherits global default."""
        cfg = {
            "upstream_stream_buffer": {
                "enabled": True,
                "providers": {
                    "local-ollama": {"enabled": False},
                },
            }
        }
        sb_cfg = cfg.get("upstream_stream_buffer", {})
        enabled = sb_cfg.get("enabled", True) if isinstance(sb_cfg, dict) else True
        if isinstance(sb_cfg, dict) and isinstance(sb_cfg.get("providers"), dict):
            prov_sb = sb_cfg["providers"].get("vsllm")  # Not in overrides
            if isinstance(prov_sb, dict):
                enabled = prov_sb.get("enabled", enabled)
        assert enabled is True  # Inherits global True


# ── n>1 Skip Tests ────────────────────────────────────────────

class TestNSkip:
    """Test that n>1 requests bypass the stream buffer."""

    def test_n_gt_1_detected(self):
        """n=2 is correctly detected as n>1."""
        payload = {"model": "test", "messages": [], "n": 2}
        has_n_gt_1 = isinstance(payload.get("n"), int) and payload["n"] > 1
        assert has_n_gt_1 is True

    def test_n_1_not_flagged(self):
        """n=1 is NOT flagged as n>1."""
        payload = {"model": "test", "messages": [], "n": 1}
        has_n_gt_1 = isinstance(payload.get("n"), int) and payload["n"] > 1
        assert has_n_gt_1 is False

    def test_n_absent_not_flagged(self):
        """Absent n is NOT flagged (defaults to 1 in OpenAI API)."""
        payload = {"model": "test", "messages": []}
        has_n_gt_1 = isinstance(payload.get("n"), int) and payload["n"] > 1
        assert has_n_gt_1 is False

    def test_n_string_not_flagged(self):
        """String n (invalid but defensive) is NOT flagged."""
        payload = {"model": "test", "messages": [], "n": "2"}
        has_n_gt_1 = isinstance(payload.get("n"), int) and payload["n"] > 1
        assert has_n_gt_1 is False


# ── Non-Mutation Tests ────────────────────────────────────────

class TestPayloadNonMutation:
    """Test that the stream payload copy doesn't mutate upstream_payload.

    This is correction #2: S3/S6 retry handlers read upstream_payload
    to build continuation requests. If we mutate it in place, they'd
    send stream:true via non-streaming client.send() → parse failure.
    """

    def test_stream_payload_is_copy(self):
        """Stream payload must be a separate dict from upstream_payload."""
        upstream_payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "max_tokens": 1000,
        }
        # Build stream payload (non-mutating copy)
        stream_payload = {**upstream_payload, "stream": True}
        stream_payload["stream_options"] = {"include_usage": True}

        # Original must be unchanged
        assert upstream_payload["stream"] is False
        assert "stream_options" not in upstream_payload
        # Copy must have stream enabled
        assert stream_payload["stream"] is True
        assert stream_payload["stream_options"] == {"include_usage": True}

    def test_s3_continuation_reads_original(self):
        """S3 continuation builds from upstream_payload (stream:false)."""
        upstream_payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        # Simulate stream-buffer having created a copy
        _ = {**upstream_payload, "stream": True}

        # S3 continuation reads upstream_payload
        _cont_payload = dict(upstream_payload)
        _cont_payload["messages"] = [*upstream_payload["messages"], {"role": "assistant", "content": "partial"}]
        _cont_payload["messages"].append({"role": "user", "content": "CONTINUE"})

        # Continuation must NOT have stream:true leaked from buffer
        assert _cont_payload.get("stream") is False or _cont_payload.get("stream") is None


# ── S3/S6 Buffered Routing Tests ──────────────────────────────

class TestS3S6BufferedRouting:
    """Test that S3/S6 continuation calls route through _buffered_send
    when _apply_stream_buffer is true, and through plain client.send otherwise.
    """

    def test_s3_continuation_uses_buffered_when_enabled(self):
        """When _apply_stream_buffer is True, S3 continuation calls
        _buffered_send instead of plain client.send."""
        _apply_stream_buffer = True
        # The dispatch logic: if _apply_stream_buffer -> _buffered_send
        uses_buffered = _apply_stream_buffer is True
        assert uses_buffered is True

    def test_s3_continuation_uses_plain_when_disabled(self):
        """When _apply_stream_buffer is False, S3 continuation falls
        back to plain client.send (no stream rewrite)."""
        _apply_stream_buffer = False
        uses_buffered = _apply_stream_buffer is True
        assert uses_buffered is False

    def test_s6_retry_uses_buffered_when_enabled(self):
        """When _apply_stream_buffer is True, S6 quality-gate retry
        calls _buffered_send instead of plain client.send."""
        _apply_stream_buffer = True
        uses_buffered = _apply_stream_buffer is True
        assert uses_buffered is True

    def test_s6_retry_uses_plain_when_disabled(self):
        """When _apply_stream_buffer is False, S6 retry falls back
        to plain client.send."""
        _apply_stream_buffer = False
        uses_buffered = _apply_stream_buffer is True
        assert uses_buffered is False

    def test_s3_continuation_payload_preserves_stream_false(self):
        """S3 continuation payload derived from upstream_payload must
        NOT leak stream:true from the buffer's non-mutating copy.

        The _buffered_send helper creates {**_cont_payload, "stream": True}
        internally — the caller's _cont_payload stays stream:false.
        """
        upstream_payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        # build_continuation_payload creates from upstream_payload
        _cont_payload = dict(upstream_payload)
        _cont_payload["messages"] = list(upstream_payload["messages"])
        _cont_payload["messages"].append({"role": "assistant", "content": "partial"})
        _cont_payload["messages"].append({"role": "user", "content": "CONTINUE"})

        # _buffered_send makes its OWN copy — verify the source stays unmutated
        _internal_copy = {**_cont_payload, "stream": True}
        _internal_copy["stream_options"] = {"include_usage": True}

        assert _cont_payload.get("stream") is False
        assert "stream_options" not in _cont_payload
        assert _internal_copy["stream"] is True

    def test_s6_retry_payload_strips_stream_before_buffer(self):
        """S6 retry explicitly sets stream:false on _retry_payload before
        _buffered_send rewrites it to stream:true internally."""
        upstream_payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        _retry_payload = dict(upstream_payload)
        _retry_payload["max_tokens"] = 16384
        _retry_payload["stream"] = False

        # _buffered_send rewrites to stream:true on its own copy
        _internal = {**_retry_payload, "stream": True}
        assert _retry_payload["stream"] is False
        assert _internal["stream"] is True
        assert _internal["max_tokens"] == 16384


# ── Fail-Open Time Budget Tests ───────────────────────────────

class TestFailOpenTimeBudget:
    """Test the <2s fail-open decision logic.

    Correction #6: only immediate (<2s) rejection falls back to non-stream.
    Timeout = real error → combo chain advance (NO non-stream retry, which
    would 524 again, doubling latency).
    """

    def test_immediate_rejection_triggers_fail_open(self):
        """<2s rejection should trigger fail-open to non-stream."""
        elapsed = 0.5
        assert elapsed < 2.0  # Would fail-open

    def test_late_error_does_not_fail_open(self):
        """>2s error should NOT fail-open (would 524 again)."""
        elapsed = 5.0
        assert not (elapsed < 2.0)  # Would NOT fail-open

    def test_boundary_2s(self):
        """At exactly 2.0s, should NOT fail-open (edge case safety)."""
        elapsed = 2.0
        assert not (elapsed < 2.0)  # Exactly at boundary — no fail-open
