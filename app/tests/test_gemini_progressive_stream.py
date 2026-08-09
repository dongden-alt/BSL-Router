"""Deterministic Antigravity progressive Gemini SSE regression tests."""
import asyncio
import builtins
import io
import json
import os
import sys

import httpx
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main


class _ControlledStream(httpx.AsyncByteStream):
    def __init__(self, complete):
        self.complete = complete
        self.closed = False

    async def __aiter__(self):
        yield (
            b'data: {"id":"chunk-1","model":"fake-model","choices":['
            b'{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
        )
        await self.complete.wait()
        yield (
            b'data: {"id":"chunk-1","model":"fake-model","choices":['
            b'{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        )

    async def aclose(self):
        self.closed = True


class _FakeStreamingClient:
    def __init__(self, complete):
        self.complete = complete
        self.sent = False
        self.response = None

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.sent = True
        self.response = httpx.Response(200, request=request, stream=_ControlledStream(self.complete))
        return self.response


class _FakeErrorClient:
    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        return httpx.Response(503, request=request, content=b"upstream unavailable")


def _request(body):
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        # The live ASGI server does not signal disconnect merely because it has
        # delivered the complete request body. Keep the request connected while
        # the egress generator polls request.is_disconnected().
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1internal:streamGenerateContent",
            "query_string": b"alt=sse",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        receive,
    )


def _antigravity_body():
    return {
        "project": "test-project",
        "model": "fake-model",
        "userAgent": "antigravity",
        "request": {"contents": [{"role": "user", "parts": [{"text": "Say hello"}]}]},
    }


def _config():
    return {
        "providers": {
            "fake": {
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://fake.invalid"}],
                "models": [{"id": "fake-model", "enabled": True}],
            }
        },
        "aliases": {},
        "combos": [],
        "antigravity_integration": {
            "enabled": True,
            "mappings": {"fake-model": "fake/fake-model"},
        },
        "tools": {"output_thinking_squeeze": False},
    }


def _block_forensics(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        normalized = str(path).replace("\\", "/")
        if normalized.endswith((".brain/logs/antigravity_inbound.jsonl", ".brain/logs/outbound_upstream.jsonl")):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)
    monkeypatch.setattr(main._os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "test-request")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: None)


def test_antigravity_stream_returns_headers_and_heartbeat_before_upstream_completion(monkeypatch):
    complete = asyncio.Event()
    client = _FakeStreamingClient(complete)
    _block_forensics(monkeypatch)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.antigravity_generate(_request(_antigravity_body()))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert client.sent is False

        iterator = response.body_iterator
        assert await anext(iterator) == b": heartbeat\n\n"
        assert client.sent is False

        content = await anext(iterator)
        assert complete.is_set() is False
        assert content.startswith(b"data: ")
        payload = json.loads(content[6:])
        # Cloud Code envelope required by Antigravity IDE (not bare candidates).
        assert "response" in payload
        assert payload["response"]["candidates"][0]["content"]["parts"] == [{"text": "Hello"}]
        assert "modelVersion" in payload["response"]
        assert "responseId" in payload["response"]

        complete.set()
        finish = await anext(iterator)
        finish_payload = json.loads(finish[6:])
        assert "response" in finish_payload
        assert finish_payload["response"]["candidates"][0]["finishReason"] == "STOP"
        assert await anext(iterator) == b"data: [DONE]\n\n"

    asyncio.run(scenario())


def test_antigravity_stream_upstream_error_is_valid_gemini_sse_then_done(monkeypatch):
    _block_forensics(monkeypatch)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: _FakeErrorClient())

    async def scenario():
        response = await main.antigravity_generate(_request(_antigravity_body()))
        iterator = response.body_iterator
        assert await anext(iterator) == b": heartbeat\n\n"
        # FREEZE FIX (2026-08-07): the terminal contract is now a SOLE
        # finishReason-bearing candidate frame — NO preceding bare
        # {"error":...} frame. The old error -> terminal -> [DONE] sequence
        # poisoned the Antigravity Gemini parser (a top-level error object
        # makes it stop consuming the later finishReason candidate), which is
        # what froze the IDE on 2026-08-07. The error text stays VISIBLE in
        # the candidate's parts; only the discarded bare-error wrapper is gone.
        terminal = json.loads((await anext(iterator))[6:])
        assert "error" not in terminal, "bare top-level error frame must not precede the terminal candidate"
        candidate = terminal["response"]["candidates"][0]
        assert candidate["finishReason"] == "STOP", "no finishReason: client cannot end the stream"
        assert "503" in candidate["content"]["parts"][0]["text"], "error must stay visible to the user"
        assert "upstream unavailable" in candidate["content"]["parts"][0]["text"], "error message must stay visible"
        assert await anext(iterator) == b"data: [DONE]\n\n"

    asyncio.run(scenario())


async def _next_data_frame(iterator):
    """Return the next real SSE frame, skipping keepalive comment frames."""
    while True:
        frame = await anext(iterator)
        if frame != b": keepalive\n\n":
            return frame


def test_antigravity_stream_emits_keepalive_during_upstream_thinking_gap(monkeypatch):
    """Regression: reasoning models (opus-*-thinking via vietapi) send an early
    frame then withhold ALL content bytes server-side for tens of seconds while
    thinking. During that gap the egress must keep the client socket warm with
    SSE keepalive comment frames instead of going silent (the observed IDE
    "frozen screen"). The controlled stream holds its finish chunk behind an
    asyncio.Event — a deterministic stand-in for the upstream thinking gap — and
    the keepalive interval is shrunk so the proof does not depend on real timing.
    """
    complete = asyncio.Event()
    client = _FakeStreamingClient(complete)
    _block_forensics(monkeypatch)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)
    # Shrink cadence so the gap deterministically trips the keepalive timer.
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.05)

    async def scenario():
        response = await main.antigravity_generate(_request(_antigravity_body()))
        iterator = response.body_iterator
        assert await anext(iterator) == b": heartbeat\n\n"

        # First upstream content chunk translates through immediately.
        content = await _next_data_frame(iterator)
        payload = json.loads(content[6:])
        assert "response" in payload
        assert payload["response"]["candidates"][0]["content"]["parts"] == [{"text": "Hello"}]

        # Upstream now withholds the finish chunk (thinking gap). The egress must
        # emit at least one keepalive comment frame rather than blocking silently.
        assert complete.is_set() is False
        assert await anext(iterator) == b": keepalive\n\n"

        # Release the upstream; the real finish + DONE still arrive intact,
        # skipping any additional keepalives emitted before the finish lands.
        complete.set()
        finish = await _next_data_frame(iterator)
        finish_payload = json.loads(finish[6:])
        assert "response" in finish_payload
        assert finish_payload["response"]["candidates"][0]["finishReason"] == "STOP"
        assert await _next_data_frame(iterator) == b"data: [DONE]\n\n"

    asyncio.run(scenario())


def test_antigravity_stream_cancellation_closes_upstream_response(monkeypatch):
    complete = asyncio.Event()
    client = _FakeStreamingClient(complete)
    _block_forensics(monkeypatch)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.antigravity_generate(_request(_antigravity_body()))
        iterator = response.body_iterator
        assert await anext(iterator) == b": heartbeat\n\n"
        assert (await _next_data_frame(iterator)).startswith(b"data: ")
        await iterator.aclose()
        assert client.response.stream.closed is True

    asyncio.run(scenario())
