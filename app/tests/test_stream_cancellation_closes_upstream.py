"""Regression coverage: client disconnect MUST close the upstream response.

LEAK FIX (2026-08-02). `async for` does NOT call aclose() on its iterator when
the loop exits early. Every streaming wrapper therefore needs an explicit
`finally: await source.aclose()` so a downstream cancellation propagates all the
way to the egress generator's finally, which closes the upstream httpx response.

Without it each aborted request leaks one upstream connection; enough of them
exhaust the keepalive pool and wedge the router until restart.

The Gemini path was already covered by test_gemini_progressive_stream.py. The
OpenAI (raw passthrough) and Anthropic->OpenAI egress paths were NOT, which is
exactly why their leaks survived undetected. These tests close that gap.
"""

import asyncio
import builtins
import io
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main


class _OpenAIStream(httpx.AsyncByteStream):
    """Emits one chunk, then blocks forever. Records whether it was closed."""

    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield (
            b'data: {"id":"c1","model":"fake-model","choices":['
            b'{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
        )
        await asyncio.sleep(3600)

    async def aclose(self):
        self.closed = True


class _AnthropicStream(httpx.AsyncByteStream):
    """Anthropic-format SSE: one text delta, then blocks forever."""

    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"m1","model":"fake-model",'
            b'"role":"assistant","content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        )
        yield (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"Hello"}}\n\n'
        )
        await asyncio.sleep(3600)

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self, stream):
        self.stream = stream
        self.response = None

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.response = httpx.Response(200, request=request, stream=self.stream)
        return self.response


def _block_forensics(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)


def _config(fmt):
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "fake": {
                "type": "custom",
                "format": fmt,
                "connections": [{
                    "enabled": True,
                    "api_key": "test",
                    "base_url": "https://fake.invalid",
                }],
                "models": [{"id": "fake-model", "enabled": True, "thinking": "off"}],
            }
        },
        "aliases": {},
        "combos": [],
    }


def _install(monkeypatch, client, fmt):
    _block_forensics(monkeypatch)
    cs.replace_config(_config(fmt))
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: None)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "req-1")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: None)


async def _first_data_frame(iterator):
    """Advance past SSE comment frames to the first real data frame."""
    async for chunk in iterator:
        if chunk.startswith(b"data:"):
            return chunk
    raise AssertionError("stream ended before any data frame")


def test_openai_stream_cancellation_closes_upstream(monkeypatch):
    """raw_upstream_guarded must close its source on client disconnect."""
    stream = _OpenAIStream()
    client = _FakeClient(stream)
    _install(monkeypatch, client, "openai")

    async def scenario():
        response = await main._process_chat_completion({
            "model": "fake/fake-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
        iterator = response.body_iterator
        assert (await _first_data_frame(iterator)).startswith(b"data:")
        # Client goes away mid-stream.
        await iterator.aclose()
        assert stream.closed is True, (
            "upstream stream was NOT closed on client disconnect - "
            "connection leaked (see LEAK FIX 2026-08-02)"
        )

    asyncio.run(scenario())


def test_anthropic_egress_cancellation_closes_upstream(monkeypatch):
    """anthropic_to_openai_egress_stream_guarded must close its source."""
    stream = _AnthropicStream()
    client = _FakeClient(stream)
    _install(monkeypatch, client, "anthropic")

    async def scenario():
        response = await main._process_chat_completion({
            "model": "fake/fake-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
        iterator = response.body_iterator
        assert (await _first_data_frame(iterator)).startswith(b"data:")
        await iterator.aclose()
        assert stream.closed is True, (
            "upstream stream was NOT closed on client disconnect - "
            "connection leaked (see LEAK FIX 2026-08-02)"
        )

    asyncio.run(scenario())
