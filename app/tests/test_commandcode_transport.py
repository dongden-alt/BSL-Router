"""Tests for the CommandCode upstream adapter (Vercel AI SDK transport).

Covers:
- Envelope builder: OpenAI payload → {threadId, memory, config, params} shell
- Message normalization: string content → Anthropic content-block shape
- Vercel AI SSE translator: text-delta → OpenAI chunks, finish-step/finish →
  finish_reason + usage, ignored frames, non-stream aggregation
- Client facade: build_request rewrites URL to /alpha/generate + wraps payload;
  send translates stream and aggregates non-stream
- Fail-open on malformed frames
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.compat.adapters.commandcode_upstream import (
    COMMANDCODE_GENERATE_PATH,
    VercelAISSETranslator,
    openai_to_commandcode_envelope,
    _normalize_messages,
)


# ── Envelope builder ─────────────────────────────────────────────────────────

class TestEnvelope:
    def test_basic_envelope_shape(self):
        payload = {
            "model": "deepseek/deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
            "max_tokens": 1024,
            "temperature": 0.7,
        }
        env = openai_to_commandcode_envelope(payload, "deepseek/deepseek-v4-flash")
        assert "threadId" in env
        assert env["memory"] == ""
        assert "config" in env
        assert env["config"]["environment"] == "win32"
        params = env["params"]
        assert params["model"] == "deepseek/deepseek-v4-flash"
        assert params["stream"] is True
        assert params["max_tokens"] == 1024
        assert params["temperature"] == 0.7
        assert len(params["messages"]) == 1

    def test_config_block_types_are_arrays(self):
        """Upstream validator 400s on string structure/recentCommits."""
        env = openai_to_commandcode_envelope({"model": "m", "messages": []}, "m")
        cfg = env["config"]
        assert cfg["environment"] == "win32"
        assert isinstance(cfg["structure"], list)
        assert isinstance(cfg["recentCommits"], list)
        assert cfg["isGitRepo"] is True

    def test_optional_params_omitted_when_absent(self):
        payload = {"model": "m", "messages": []}
        env = openai_to_commandcode_envelope(payload, "m")
        params = env["params"]
        assert "max_tokens" not in params
        assert "temperature" not in params
        assert "top_p" not in params
        assert "stop" not in params

    def test_stream_forced_true_on_wire(self):
        """CLI-only lane fingerprints stream:false as proxying — always stream."""
        env = openai_to_commandcode_envelope({"model": "m", "messages": []}, "m")
        assert env["params"]["stream"] is True
        env2 = openai_to_commandcode_envelope(
            {"model": "m", "messages": [], "stream": False}, "m"
        )
        assert env2["params"]["stream"] is True


# ── Message normalization ────────────────────────────────────────────────────

class TestNormalizeMessages:
    def test_string_content_becomes_block(self):
        msgs = [{"role": "user", "content": "Hello world"}]
        out = _normalize_messages(msgs)
        assert out == [{"role": "user", "content": [{"type": "text", "text": "Hello world"}]}]

    def test_list_content_passes_through(self):
        blocks = [{"type": "text", "text": "already blocks"}]
        msgs = [{"role": "assistant", "content": blocks}]
        out = _normalize_messages(msgs)
        assert out[0]["content"] is blocks

    def test_none_content_becomes_empty_text(self):
        out = _normalize_messages([{"role": "user", "content": None}])
        assert out[0]["content"] == [{"type": "text", "text": ""}]

    def test_non_dict_message_coerced(self):
        out = _normalize_messages(["just a string"])
        assert out == [{"role": "user", "content": [{"type": "text", "text": "just a string"}]}]

    def test_missing_role_defaults_user(self):
        out = _normalize_messages([{"content": "hi"}])
        assert out[0]["role"] == "user"

    def test_empty_list(self):
        assert _normalize_messages([]) == []


# ── SSE helpers ──────────────────────────────────────────────────────────────

def _sse(frame: dict) -> str:
    return f"data: {json.dumps(frame)}\n\n"


def _stream(frames: list) -> str:
    return "".join(_sse(f) for f in frames)


# ── Vercel AI SSE translator ─────────────────────────────────────────────────

class TestTranslator:
    def test_text_deltas_become_content_chunks(self):
        tr = VercelAISSETranslator("test-model")
        raw = _stream([
            {"type": "start"},
            {"type": "start-step", "request": {}},
            {"type": "text-start", "id": "t1"},
            {"type": "text-delta", "id": "t1", "text": "Hello"},
            {"type": "text-delta", "id": "t1", "text": " world"},
            {"type": "text-end", "id": "t1"},
            {"type": "finish-step", "finishReason": "stop",
             "usage": {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15}},
            {"type": "finish", "totalUsage": {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15}},
            {"type": "provider-metadata"},
        ])
        out = tr.feed(raw) + tr.close()
        text = b"".join(out).decode("utf-8")
        chunks = [json.loads(line[5:]) for line in text.split("\n") if line.startswith("data:") and line[5:].strip() != "[DONE]"]

        # First content chunk should have role
        content_chunks = [c for c in chunks if c["choices"][0]["delta"].get("content")]
        assert len(content_chunks) == 2
        assert content_chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert content_chunks[0]["choices"][0]["delta"]["content"] == "Hello"
        assert content_chunks[1]["choices"][0]["delta"]["content"] == " world"
        assert "role" not in content_chunks[1]["choices"][0]["delta"]

        # Finish chunk
        finish_chunks = [c for c in chunks if c["choices"][0].get("finish_reason")]
        assert len(finish_chunks) >= 1
        assert finish_chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert finish_chunks[-1]["usage"]["total_tokens"] == 15

        # DONE sentinel
        assert "data: [DONE]" in text

    def test_ignored_frames_produce_no_output(self):
        tr = VercelAISSETranslator("m")
        out = tr.feed(_sse({"type": "start"}))
        out += tr.feed(_sse({"type": "start-step", "request": {}}))
        out += tr.feed(_sse({"type": "text-start", "id": "x"}))
        out += tr.feed(_sse({"type": "text-end", "id": "x"}))
        out += tr.feed(_sse({"type": "provider-metadata"}))
        assert out == []

    def test_finish_reason_mapping(self):
        tr = VercelAISSETranslator("m")
        out = tr.feed(_sse({"type": "finish", "finishReason": "length",
                            "totalUsage": {"promptTokens": 1, "completionTokens": 2, "totalTokens": 3}}))
        text = b"".join(out).decode()
        assert '"finish_reason": "length"' in text

    def test_cache_hit_tokens_preserved(self):
        tr = VercelAISSETranslator("m")
        out = tr.feed(_sse({"type": "finish", "finishReason": "stop",
                            "totalUsage": {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15,
                                           "raw": {"prompt_cache_hit_tokens": 8}}}))
        text = b"".join(out).decode()
        assert '"prompt_cache_hit_tokens": 8' in text

    def test_partial_line_buffering(self):
        """Frames split across feed() calls should still parse."""
        tr = VercelAISSETranslator("m")
        frame = _sse({"type": "text-delta", "id": "t", "text": "split"})
        mid = len(frame) // 2
        out1 = tr.feed(frame[:mid])
        out2 = tr.feed(frame[mid:])
        text = b"".join(out1 + out2).decode()
        assert '"content": "split"' in text

    def test_malformed_frame_fail_open(self):
        tr = VercelAISSETranslator("m")
        out = tr.feed("data: {not json}\n\n")
        out += tr.feed("data: [DONE]\n\n")
        out += tr.close()
        text = b"".join(out).decode()
        # Should not crash; [DONE] sentinel from close() still present
        assert "data: [DONE]" in text

    def test_close_flushes_trailing_buffer(self):
        tr = VercelAISSETranslator("m")
        # Strip one trailing \n: frame completes when the next feed/close
        # supplies the missing newline boundary.
        out = tr.feed(_sse({"type": "text-delta", "id": "t", "text": "tail"})[:-1])
        out += tr.close()
        text = b"".join(out).decode()
        assert '"content": "tail"' in text
        assert "data: [DONE]" in text


# ── Non-stream aggregation ───────────────────────────────────────────────────

class TestAggregation:
    def test_final_openai_response(self):
        tr = VercelAISSETranslator("agg-model")
        raw = _stream([
            {"type": "text-delta", "id": "t", "text": "Hello "},
            {"type": "text-delta", "id": "t", "text": "world"},
            {"type": "finish", "finishReason": "stop",
             "totalUsage": {"promptTokens": 20, "completionTokens": 10, "totalTokens": 30}},
        ])
        tr.feed(raw)
        final = tr.final_openai_response()
        assert final["object"] == "chat.completion"
        assert final["model"] == "agg-model"
        assert final["choices"][0]["message"]["content"] == "Hello world"
        assert final["choices"][0]["message"]["role"] == "assistant"
        assert final["choices"][0]["finish_reason"] == "stop"
        assert final["usage"]["total_tokens"] == 30

    def test_empty_response_defaults(self):
        tr = VercelAISSETranslator("m")
        final = tr.final_openai_response()
        assert final["choices"][0]["message"]["content"] == ""
        assert final["choices"][0]["finish_reason"] == "stop"
        assert final["usage"]["total_tokens"] == 0


# ── Client facade (build_request + send) ─────────────────────────────────────

class _MockStreamResponse:
    """Fake streaming response: aiter_raw works (real httpx.Response with
    content=bytes is pre-consumed and raises StreamConsumed on aiter_raw)."""

    def __init__(self, body: bytes, status_code: int = 200):
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream"}
        self._body = body

    async def aiter_raw(self):
        # Yield in two chunks to exercise partial-line buffering.
        mid = len(self._body) // 2
        yield self._body[:mid]
        yield self._body[mid:]

    async def aiter_bytes(self):
        async for chunk in self.aiter_raw():
            yield chunk

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        pass


class _MockInnerClient:
    """Minimal httpx.AsyncClient stand-in for testing the wrapper."""

    def __init__(self, response_body: bytes = b"", status_code: int = 200):
        self._response_body = response_body
        self._status_code = status_code
        self.last_request = None

    def build_request(self, method, url, headers=None, content=None, **kw):
        import httpx
        req = httpx.Request(method, url, headers=headers, content=content)
        self.last_request = req
        return req

    async def send(self, request, *, stream=False, **kw):
        return _MockStreamResponse(self._response_body, self._status_code)


class TestClientFacade:
    def test_build_request_rewrites_url_and_envelope(self):
        from app.compat.adapters.commandcode_upstream import wrap_commandcode_upstream_client

        inner = _MockInnerClient()
        client = wrap_commandcode_upstream_client(inner, "fallback-model")
        payload = json.dumps({
            "model": "deepseek/deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }).encode()

        req = client.build_request(
            "POST",
            "https://api.commandcode.ai/alpha/generate",
            headers={"Authorization": "Bearer test"},
            content=payload,
        )
        assert COMMANDCODE_GENERATE_PATH in str(req.url)
        body = json.loads(req.content.decode())
        assert body["params"]["model"] == "deepseek/deepseek-v4-flash"
        assert body["params"]["stream"] is True
        assert body["params"]["messages"][0]["content"] == [{"type": "text", "text": "Hi"}]
        assert req.headers["Accept"] == "text/event-stream"

    def test_build_request_fallback_model(self):
        from app.compat.adapters.commandcode_upstream import wrap_commandcode_upstream_client

        inner = _MockInnerClient()
        client = wrap_commandcode_upstream_client(inner, "my-default")
        payload = json.dumps({"messages": []}).encode()
        req = client.build_request("POST", "https://api.commandcode.ai/alpha/generate",
                                   headers={}, content=payload)
        body = json.loads(req.content.decode())
        assert body["params"]["model"] == "my-default"

    def test_send_non_stream_aggregates(self):
        from app.compat.adapters.commandcode_upstream import wrap_commandcode_upstream_client

        frames = _stream([
            {"type": "text-delta", "id": "t", "text": "Answer"},
            {"type": "finish", "finishReason": "stop",
             "totalUsage": {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}},
        ])
        inner = _MockInnerClient(response_body=frames.encode())
        client = wrap_commandcode_upstream_client(inner, "m")
        req = client.build_request("POST", "https://api.commandcode.ai/alpha/generate",
                                   headers={}, content=json.dumps({"model": "m", "messages": []}).encode())
        resp = asyncio.run(client.send(req, stream=False))
        assert resp.status_code == 200
        data = json.loads(resp.content.decode())
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Answer"
        assert data["usage"]["total_tokens"] == 8

    def test_send_non_200_passthrough(self):
        from app.compat.adapters.commandcode_upstream import wrap_commandcode_upstream_client

        inner = _MockInnerClient(response_body=b'{"error":"forbidden"}', status_code=403)
        client = wrap_commandcode_upstream_client(inner, "m")
        req = client.build_request("POST", "https://api.commandcode.ai/alpha/generate",
                                   headers={}, content=json.dumps({"model": "m", "messages": []}).encode())
        resp = asyncio.run(client.send(req, stream=False))
        assert resp.status_code == 403
