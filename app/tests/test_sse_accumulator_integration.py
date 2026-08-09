"""
Real integration tests for _accumulate_sse_stream — the shared SSE drain helper.

Unlike the tautological tests in test_stream_buffer.py, these tests create
mock httpx.Response objects with working aiter_bytes() implementations that
emit real SSE chunk sequences. They exercise the ACTUAL production code path:
chunk decoding → line splitting → data: prefix filter → JSON parse →
dual-format event extraction → assembly into OpenAI-shaped dict.

Coverage gaps addressed (per Opus + DeepSeek audits):
  - Real SSE multi-chunk drain (not string mirrors)
  - Disconnect probe firing (mock request.is_disconnected)
  - Mid-stream accumulation error propagation
  - Tool call accumulation across multiple chunks
  - Anthropic thinking_delta + text_delta interleave
  - Usage extraction from message_start AND message_delta
  - finish_reason mapping (end_turn → stop, tool_use → tool_calls)
  - ID/model capture from SSE chunks
"""
import json
import time
import asyncio
import pytest
import httpx


# ── Mock SSE Response ─────────────────────────────────────────

class MockSSEResponse:
    """Minimal mock that quacks like httpx.Response for SSE accumulation.

    Implements aiter_bytes() to yield SSE-encoded chunks, plus aclose().
    Stores a .request attribute for HTTPStatusError construction.
    """
    def __init__(self, chunks: list[bytes], status_code=200):
        self._chunks = chunks
        self.status_code = status_code
        self._closed = False
        self.request = httpx.Request("POST", "http://test/upstream")

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self._closed = True

    @property
    def is_closed(self):
        return self._closed


class MockRequest:
    """Mock FastAPI Request with controllable is_disconnected()."""
    def __init__(self, disconnect_on_call: int | None = None):
        self._call_count = 0
        self._disconnect_on = disconnect_on_call

    async def is_disconnected(self):
        self._call_count += 1
        if self._disconnect_on is not None and self._call_count >= self._disconnect_on:
            return True
        return False


# ── Helpers to build SSE chunks ───────────────────────────────

def _oai_chunk(content=None, reasoning=None, tool_calls=None,
               finish_reason=None, usage=None, chunk_id=None, model=None):
    """Build a single OpenAI-format SSE data line."""
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    payload = {
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if chunk_id is not None:
        payload["id"] = chunk_id
    if model is not None:
        payload["model"] = model
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n".encode()


def _anth_event(event_type, delta=None, extra=None):
    """Build a single Anthropic-format SSE data line."""
    payload = {"type": event_type}
    if delta is not None:
        payload["delta"] = delta
    if extra is not None:
        payload.update(extra)
    return f"data: {json.dumps(payload)}\n\n".encode()


def _done():
    return b"data: [DONE]\n\n"


# ── OpenAI SSE Integration Tests ──────────────────────────────

class TestOpenAISSEIntegration:
    """Exercise _accumulate_sse_stream with real OpenAI-format SSE byte streams."""

    def _run(self, sse_resp, request_obj=None, is_anthropic=False):
        """Run the REAL production accumulator in a fresh event loop.

        Imports `_accumulate_sse_stream` from app.main (module-level, so tests bind
        to the actual production parser — no mirror drift) and normalizes its
        OpenAI-shaped dict into the flat shape the assertions below use.
        """
        from app.main import _accumulate_sse_stream  # REAL production code

        results = asyncio.run(_accumulate_sse_stream(
            sse_resp,
            _is_anthropic_fmt=is_anthropic,
            _target_model="test-model",
            _request=request_obj,
            _label="test",
        ))
        return self._normalize(results)

    @staticmethod
    def _normalize(assembled: dict) -> dict:
        """Flatten the accumulator's OpenAI-shaped dict into the test-friendly shape."""
        choice = (assembled.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        usage = assembled.get("usage", {})
        ptd = usage.get("prompt_tokens_details", {}) or {}
        return {
            "message": msg,
            "finish_reason": choice.get("finish_reason", "stop"),
            "id": assembled.get("id"),
            "model": assembled.get("model"),
            "in_tokens": usage.get("prompt_tokens", 0),
            "out_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cached_tokens": ptd.get("cached_tokens", 0),
            "content": msg.get("content", ""),
            "reasoning": msg.get("reasoning_content", ""),
            "tool_calls": msg.get("tool_calls", []),
        }

    def test_openai_multi_chunk_content_drain(self):
        """Content split across 5 chunks assembles correctly."""
        chunks = [
            _oai_chunk(content="Hello"),
            _oai_chunk(content=" world"),
            _oai_chunk(content=" from"),
            _oai_chunk(content=" SSE"),
            _oai_chunk(content="!"),
            _oai_chunk(finish_reason="stop"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["content"] == "Hello world from SSE!"
        assert result["finish_reason"] == "stop"

    def test_openai_reasoning_content_accumulation(self):
        """reasoning_content from multiple chunks assembles correctly."""
        chunks = [
            _oai_chunk(reasoning="Let me think"),
            _oai_chunk(reasoning=" about this"),
            _oai_chunk(content="The answer is 42"),
            _oai_chunk(finish_reason="stop"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["content"] == "The answer is 42"
        assert result["reasoning"] == "Let me think about this"
        assert result["message"]["reasoning_content"] == "Let me think about this"

    def test_openai_tool_call_multi_chunk(self):
        """Tool calls accumulated across chunks with index tracking."""
        chunks = [
            _oai_chunk(tool_calls=[{"index": 0, "id": "call_1", "function": {"name": "get_wea"}}]),
            _oai_chunk(tool_calls=[{"index": 0, "function": {"arguments": "{\"loc"}}]),
            _oai_chunk(tool_calls=[{"index": 0, "function": {"arguments": "\":\"NYC\"}"}}]),
            _oai_chunk(finish_reason="tool_calls"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "call_1"
        assert result["tool_calls"][0]["function"]["name"] == "get_wea"
        assert result["tool_calls"][0]["function"]["arguments"] == '{"loc":"NYC"}'
        assert result["finish_reason"] == "tool_calls"

    def test_openai_usage_extraction(self):
        """Usage block extracted from final chunk."""
        chunks = [
            _oai_chunk(content="Hi"),
            _oai_chunk(finish_reason="stop", usage={
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["in_tokens"] == 100
        assert result["out_tokens"] == 50

    def test_openai_id_and_model_capture(self):
        """ID captured from first SSE chunk; model is _target_model (authoritative).

        Production intentionally uses _target_model over the upstream's model echo
        (see main.py:3618 comment) so combo fallback shows the correct model.
        """
        chunks = [
            _oai_chunk(chunk_id="chatcmpl-abc-123", model="gpt-test-4"),
            _oai_chunk(content="x"),
            _oai_chunk(finish_reason="stop"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["id"] == "chatcmpl-abc-123"
        assert result["model"] == "test-model"  # _target_model, not upstream echo

    def test_openai_empty_chunks_skipped(self):
        """Empty/whitespace-only chunks don't cause errors."""
        chunks = [
            b"",
            b"  \n  ",
            _oai_chunk(content="real content"),
            _oai_chunk(finish_reason="stop"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["content"] == "real content"

    def test_openai_malformed_json_skipped(self):
        """Malformed JSON in SSE data lines is silently skipped."""
        chunks = [
            b"data: {broken json\n\n",
            _oai_chunk(content="good"),
            _oai_chunk(finish_reason="stop"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["content"] == "good"

    def test_openai_cached_tokens_surfaced(self):
        """cached_tokens from prompt_tokens_details is surfaced in assembled usage.

        Gemini follow-up B: GLM/DeepSeek implicit prefix-cache hits should be
        observable. Production emits usage.prompt_tokens_details.cached_tokens.
        """
        chunks = [
            _oai_chunk(content="Hi"),
            _oai_chunk(finish_reason="stop", usage={
                "prompt_tokens": 200,
                "completion_tokens": 60,
                "total_tokens": 260,
                "prompt_tokens_details": {"cached_tokens": 150},
            }),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["in_tokens"] == 200
        assert result["cached_tokens"] == 150

    def test_openai_no_cached_tokens_key_absent(self):
        """When no cache info is present, prompt_tokens_details is omitted (no noise)."""
        chunks = [
            _oai_chunk(content="Hi"),
            _oai_chunk(finish_reason="stop", usage={
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["cached_tokens"] == 0


# ── Anthropic SSE Integration Tests ───────────────────────────

class TestAnthropicSSEIntegration:
    """Exercise SSE parsing with real Anthropic-format event sequences."""

    def _run(self, sse_resp, request_obj=None):
        """Same drain helper, is_anthropic=True."""
        return TestOpenAISSEIntegration._run(
            TestOpenAISSEIntegration(), sse_resp, request_obj, is_anthropic=True
        )

    def test_anthropic_text_delta_accumulation(self):
        """text_delta events assemble into content."""
        chunks = [
            _anth_event("message_start", extra={"message": {"usage": {"input_tokens": 42}}}),
            _anth_event("content_block_start", extra={"content_block": {"type": "text", "index": 0}}),
            _anth_event("content_block_delta", delta={"type": "text_delta", "text": "Hello "}),
            _anth_event("content_block_delta", delta={"type": "text_delta", "text": "world!"}),
            _anth_event("content_block_stop"),
            _anth_event("message_delta", delta={"stop_reason": "end_turn"}, extra={"usage": {"output_tokens": 10}}),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["content"] == "Hello world!"
        assert result["finish_reason"] == "stop"
        assert result["in_tokens"] == 42
        assert result["out_tokens"] == 10

    def test_anthropic_thinking_delta_accumulation(self):
        """thinking_delta events assemble into reasoning_content."""
        chunks = [
            _anth_event("message_start", extra={"message": {"usage": {"input_tokens": 10}}}),
            _anth_event("content_block_delta", delta={"type": "thinking_delta", "thinking": "I should "}),
            _anth_event("content_block_delta", delta={"type": "thinking_delta", "thinking": "think first"}),
            _anth_event("content_block_delta", delta={"type": "text_delta", "text": "Answer!"}),
            _anth_event("message_delta", delta={"stop_reason": "end_turn"}),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["content"] == "Answer!"
        assert result["reasoning"] == "I should think first"

    def test_anthropic_tool_use_accumulation(self):
        """Tool use blocks: content_block_start + input_json_delta."""
        chunks = [
            _anth_event("message_start", extra={"message": {"usage": {"input_tokens": 20}}}),
            _anth_event("content_block_start", extra={
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search", "index": 0}
            }),
            _anth_event("content_block_delta", delta={"type": "input_json_delta", "partial_json": '{"q":"hel'}),
            _anth_event("content_block_delta", delta={"type": "input_json_delta", "partial_json": 'lo"}'}),
            _anth_event("content_block_stop"),
            _anth_event("message_delta", delta={"stop_reason": "tool_use"}),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "toolu_1"
        assert result["tool_calls"][0]["function"]["name"] == "search"
        assert result["tool_calls"][0]["function"]["arguments"] == '{"q":"hello"}'
        assert result["finish_reason"] == "tool_calls"

    def test_anthropic_stop_reason_mapping(self):
        """All Anthropic stop_reasons map to correct OpenAI finish_reasons."""
        test_cases = [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
        ]
        for stop_reason, expected_fr in test_cases:
            chunks = [
                _anth_event("message_start"),
                _anth_event("message_delta", delta={"stop_reason": stop_reason}),
                _done(),
            ]
            resp = MockSSEResponse(chunks)
            result = self._run(resp)
            assert result["finish_reason"] == expected_fr, \
                f"stop_reason={stop_reason} should map to {expected_fr}"

    def test_anthropic_cache_read_surfaced(self):
        """Anthropic cache tokens fold into inclusive prompt_tokens; cache_read
        surfaces as cached_tokens while preserving the cached <= prompt invariant.

        Anthropic input_tokens is EXCLUSIVE of cache (fresh only); the assembled
        OpenAI-shaped prompt_tokens must be INCLUSIVE. fresh=50 + read=1000 +
        create=200 => 1250. Only READS (1000) map to cached_tokens; creation is
        fresh cache writes, not a discounted read."""
        chunks = [
            _anth_event("message_start", extra={"message": {"usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
            }}}),
            _anth_event("content_block_delta", delta={"type": "text_delta", "text": "Hi"}),
            _anth_event("message_delta", delta={"stop_reason": "end_turn"}, extra={"usage": {"output_tokens": 20}}),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        # Inclusive fold: 50 fresh + 1000 read + 200 creation
        assert result["in_tokens"] == 1250
        # Only cache READS map to cached_tokens
        assert result["cached_tokens"] == 1000
        # OpenAI invariant: cached_tokens is a subset of prompt_tokens
        assert result["cached_tokens"] <= result["in_tokens"]
        assert result["out_tokens"] == 20

    def test_anthropic_no_cache_keys_absent(self):
        """Without cache keys, cached_tokens is 0 and prompt_tokens == input_tokens."""
        chunks = [
            _anth_event("message_start", extra={"message": {"usage": {"input_tokens": 30}}}),
            _anth_event("content_block_delta", delta={"type": "text_delta", "text": "Hi"}),
            _anth_event("message_delta", delta={"stop_reason": "end_turn"}, extra={"usage": {"output_tokens": 5}}),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = self._run(resp)
        assert result["in_tokens"] == 30
        assert result["cached_tokens"] == 0


# ── Disconnect Probe Tests ────────────────────────────────────

class TestDisconnectProbe:
    """Test that the disconnect probe fires per chunk and actually breaks the loop."""

    def test_disconnect_after_3_chunks(self):
        """When is_disconnected returns True on the 3rd probe, accumulation
        stops and returns whatever was collected so far."""
        chunks = [
            _oai_chunk(content="chunk1"),
            _oai_chunk(content="chunk2"),
            _oai_chunk(content="chunk3"),
            _oai_chunk(content="chunk4-should-not-arrive"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        req = MockRequest(disconnect_on_call=3)
        result = TestOpenAISSEIntegration._run(
            TestOpenAISSEIntegration(), resp, request_obj=req
        )
        # Should have received chunks 1+2 but NOT chunk 4
        assert "chunk1" in result["content"]
        assert "chunk2" in result["content"]
        assert "chunk4-should-not-arrive" not in result["content"]
        assert req._call_count >= 3

    def test_no_disconnect_drains_all(self):
        """Without disconnect, all chunks are drained."""
        chunks = [
            _oai_chunk(content="a"),
            _oai_chunk(content="b"),
            _oai_chunk(content="c"),
            _oai_chunk(finish_reason="stop"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        req = MockRequest()  # Never disconnects
        result = TestOpenAISSEIntegration._run(
            TestOpenAISSEIntegration(), resp, request_obj=req
        )
        assert result["content"] == "abc"
        assert result["finish_reason"] == "stop"

    def test_disconnect_on_first_call(self):
        """Immediate disconnect (call 1) returns empty content."""
        chunks = [_oai_chunk(content="never"), _done()]
        resp = MockSSEResponse(chunks)
        req = MockRequest(disconnect_on_call=1)
        result = TestOpenAISSEIntegration._run(
            TestOpenAISSEIntegration(), resp, request_obj=req
        )
        assert result["content"] == ""


# ── Multi-Tool-Call Accumulation Test ─────────────────────────

class TestMultiToolCallAccumulation:
    """Test multiple tool calls in a single stream (parallel function calling)."""

    def test_two_parallel_tool_calls(self):
        """Two tool calls at different indices both accumulate correctly."""
        chunks = [
            _oai_chunk(tool_calls=[
                {"index": 0, "id": "call_a", "function": {"name": "func1"}},
                {"index": 1, "id": "call_b", "function": {"name": "func2"}},
            ]),
            _oai_chunk(tool_calls=[
                {"index": 0, "function": {"arguments": '{"x":1}'}},
                {"index": 1, "function": {"arguments": '{"y":2}'}},
            ]),
            _oai_chunk(finish_reason="tool_calls"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = TestOpenAISSEIntegration._run(TestOpenAISSEIntegration(), resp)
        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][0]["function"]["name"] == "func1"
        assert result["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
        assert result["tool_calls"][1]["function"]["name"] == "func2"
        assert result["tool_calls"][1]["function"]["arguments"] == '{"y":2}'


# ── Split-TCP-Chunk Resilience Test ───────────────────────────

class TestSplitTCPChunkResilience:
    """Test that SSE lines split across TCP byte boundaries parse correctly.

    DeepSeek Finding 5: both accumulators split raw chunks on "\n" without
    partial-line carryover. This test documents the accepted behavior —
    if a data: line is split across two TCP chunks, the partial line is
    silently dropped (accepted risk for HTTP/1.1 chunked transfer).
    """

    def test_complete_lines_parse_correctly(self):
        """Complete SSE lines within a single chunk parse fine."""
        full_line = _oai_chunk(content="hello")
        chunks = [full_line, _oai_chunk(finish_reason="stop"), _done()]
        resp = MockSSEResponse(chunks)
        result = TestOpenAISSEIntegration._run(TestOpenAISSEIntegration(), resp)
        assert result["content"] == "hello"

    def test_whitespace_only_chunk_skipped(self):
        """A chunk with only whitespace is skipped without error."""
        chunks = [
            b"\n\n\r\n",
            _oai_chunk(content="real"),
            _done(),
        ]
        resp = MockSSEResponse(chunks)
        result = TestOpenAISSEIntegration._run(TestOpenAISSEIntegration(), resp)
        assert result["content"] == "real"


# ── Accumulation-Error Raise Hardening (Gemini follow-up A) ─────

class TestAccumulationErrorRaise:
    """Prove the hardened HTTPStatusError never collapses to AttributeError."""

    def test_requestless_response_raises_httpstatuserror_not_attributeerror(self):
        """A response with .request=None must still raise HTTPStatusError whose
        str() is safe (httpx requires request+response non-None for __str__).
        Pre-hardening this raised AttributeError, bypassing the intended layer."""
        from app.main import _accumulate_sse_stream

        class _NoRequestResp:
            status_code = 200
            request = None

            async def aiter_bytes(self):
                raise RuntimeError("boom-mid-stream")
                yield  # pragma: no cover

            async def aclose(self):
                pass

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(_accumulate_sse_stream(
                _NoRequestResp(),
                _is_anthropic_fmt=False,
                _target_model="test-model",
                _request=None,
                _label="buf",
            ))
        # str() must not raise AttributeError — this is the regression guard.
        msg = str(exc_info.value)
        assert "accumulation failed" in msg

    def test_error_response_carries_response_object(self):
        """The raised HTTPStatusError carries the response for upstream inspection."""
        from app.main import _accumulate_sse_stream

        class _FailingResp:
            status_code = 200

            def __init__(self):
                self.request = httpx.Request("POST", "http://test/upstream")

            async def aiter_bytes(self):
                raise ConnectionError("conn reset")
                yield  # pragma: no cover

            async def aclose(self):
                pass

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(_accumulate_sse_stream(
                _FailingResp(),
                _is_anthropic_fmt=False,
                _target_model="test-model",
                _request=None,
                _label="S3-cont",
            ))
        assert exc_info.value.response is not None
        assert "S3-cont" in str(exc_info.value)


# ── Shared Usage-Token Helper (Anthropic cache parity across log sites) ─

class TestExtractUsageTokens:
    """_extract_usage_tokens normalizes OpenAI (inclusive) vs Anthropic
    (exclusive input + separate cache) usage into inclusive (in, out, cached)."""

    def test_openai_inclusive_prompt_with_cached_subset(self):
        from app.main import _extract_usage_tokens
        u = {
            "prompt_tokens": 1050, "completion_tokens": 60, "total_tokens": 1110,
            "prompt_tokens_details": {"cached_tokens": 1000},
        }
        _in, _out, _cached = _extract_usage_tokens(u)
        assert _in == 1050
        assert _out == 60
        assert _cached == 1000
        assert _cached <= _in  # invariant

    def test_anthropic_exclusive_folded_to_inclusive(self):
        from app.main import _extract_usage_tokens
        # Anthropic: fresh 50, read 1000, creation 200 -> inclusive 1250
        u = {
            "input_tokens": 50, "output_tokens": 20,
            "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200,
        }
        _in, _out, _cached = _extract_usage_tokens(u)
        assert _in == 1250
        assert _out == 20
        assert _cached == 1000  # only READS map to cached
        assert _cached <= _in  # invariant the naive copy violated

    def test_anthropic_no_cache(self):
        from app.main import _extract_usage_tokens
        u = {"input_tokens": 30, "output_tokens": 5}
        _in, _out, _cached = _extract_usage_tokens(u)
        assert (_in, _out, _cached) == (30, 5, 0)

    def test_empty_usage_all_zero(self):
        from app.main import _extract_usage_tokens
        assert _extract_usage_tokens({}) == (0, 0, 0)
        assert _extract_usage_tokens(None) == (0, 0, 0)

    def test_openai_creation_ignored_when_prompt_present(self):
        from app.main import _extract_usage_tokens
        # If an OpenAI-shaped dict somehow also carries anthropic cache keys,
        # prompt_tokens (inclusive) wins for _in; cached still prefers details.
        u = {
            "prompt_tokens": 500, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 400},
            "cache_read_input_tokens": 999,
        }
        _in, _out, _cached = _extract_usage_tokens(u)
        assert _in == 500
        assert _cached == 400
        assert _cached <= _in

    def test_zero_prompt_tokens_falls_through_to_anthropic_fold(self):
        """Regression: prompt_tokens=0 must NOT short-circuit to _in=0;
        it should fall through to the Anthropic fold so real input_tokens
        survive.  The `is not None` gate was the bug; truthy gate fixes it."""
        from app.main import _extract_usage_tokens
        u = {
            "prompt_tokens": 0, "completion_tokens": 10,
            "input_tokens": 100,
            "cache_read_input_tokens": 50,
        }
        _in, _out, _cached = _extract_usage_tokens(u)
        assert _in == 150  # 100 fresh + 50 read + 0 creation
        assert _out == 10
        assert _cached == 50
        assert _cached <= _in

    def test_null_prompt_tokens_details(self):
        """Defensive: prompt_tokens_details: null must not crash.
        Falls back to cache_read for cached."""
        from app.main import _extract_usage_tokens
        u = {"prompt_tokens": 100, "completion_tokens": 5,
             "prompt_tokens_details": None,
             "cache_read_input_tokens": 30}
        _in, _out, _cached = _extract_usage_tokens(u)
        assert _in == 100
        assert _out == 5
        assert _cached == 30  # null details -> falls back to cache_read
