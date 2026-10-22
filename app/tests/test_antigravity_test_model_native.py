"""Native antigravity OAuth test-model probe: wire contract + aggregation.

BACKGROUND (2026-09-20). POST /api/test-model for unmapped antigravity OAuth
models (e.g. ``gemini-3.6-flash-high``) returned Google's HTML 404 (robot.png
page). Root cause: the probe dialed
``{base}/v1beta/models/{model}:generateContent`` — a generativelanguage-style
path. The Cloud Code host serves ONLY ``v1internal:*`` RPC verbs, so ``/v1beta``
gets an HTML 404 from Google's edge.

The chat lane already solved this correctly: the egress client is wrapped with
``wrap_antigravity_upstream_client`` (app/compat/adapters/
antigravity_upstream.py), whose ``build_request`` rewrites the URL to
``/v1internal:streamGenerateContent?alt=sse``, forges the Cloud Code envelope
from the OpenAI payload, and sets the IDE User-Agent (REQUIRED — a generic UA
gets 403). Its non-stream ``send`` aggregates the Gemini SSE into ONE OpenAI
``chat.completion`` (choices[0].message.content, usage.prompt_tokens/
completion_tokens).

These tests pin the extracted helper ``_probe_antigravity_native_once``:
  1. the request wire hits the v1internal verb with the IDE envelope + UA
     (never /v1beta, never a literal /chat/completions dial),
  2. an upstream HTML 404 surfaces status + body unchanged,
  3. the aggregated reply is a real OpenAI completion with usage.

httpx 0.27 note: a MockTransport response built with ``text=``/``content=``
is marked stream-consumed at construction, so the wrapper's ``aiter_raw``
pass raises StreamConsumed. The 200 SSE responses are therefore built with
``stream=_BytesStream([...])`` — the same pattern as
test_antigravity_ccpa_gateway.py.
"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.main as main  # noqa: F401 — ensures _probe target module is loaded
from app.main import _probe_antigravity_native_once

# Record-then-assert: the handlers ONLY record the wire shape (path/params/
# headers/body) so test failures print the actual request, not a mock mismatch.
_RECORD: dict = {}


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


_SSE_BODY = (
    'data: {"candidates":[{"content":{"parts":[{"text":"OK"}]},"finishReason":"STOP"}],'
    '"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":1,"totalTokenCount":4}}\n\n'
).encode("utf-8")


def _sse_handler(request: httpx.Request) -> httpx.Response:
    _RECORD["url"] = str(request.url)
    _RECORD["path"] = request.url.path
    _RECORD["params"] = dict(request.url.params)
    _RECORD["headers"] = {k.lower(): v for k, v in request.headers.items()}
    _RECORD["body"] = request.content
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_BytesStream([_SSE_BODY]),
    )


def _not_found_handler(request: httpx.Request) -> httpx.Response:
    _RECORD["url"] = str(request.url)
    _RECORD["path"] = request.url.path
    _RECORD["headers"] = {k.lower(): v for k, v in request.headers.items()}
    return httpx.Response(404, text="<!DOCTYPE html><html>Error 404 (Not Found)")


def _egress_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def _probe_and_close(handler, model, token):
    client = _egress_client(handler)
    try:
        return await _probe_antigravity_native_once(client, model, token)
    finally:
        await client.aclose()


def test_probe_hits_v1internal_and_aggregates():
    status, body = asyncio.run(
        _probe_and_close(_sse_handler, "gemini-3.6-flash-high", "tok-123")
    )
    assert status == 200, f"expected 200, got {status}: {body[:200]!r}"
    j = json.loads(body)
    assert j["choices"][0]["message"]["content"] == "OK", j
    assert j["usage"]["prompt_tokens"] == 3, j["usage"]
    assert j["usage"]["completion_tokens"] == 1, j["usage"]


def test_probe_surfaces_upstream_404_html():
    status, body = asyncio.run(
        _probe_and_close(_not_found_handler, "gemini-3.6-flash-high", "tok-123")
    )
    assert status == 404, f"expected 404, got {status}"
    assert b"Error 404" in body, body[:200]


def test_probe_never_dials_v1beta_or_chat_completions():
    asyncio.run(_probe_and_close(_sse_handler, "gemini-3.6-flash-high", "tok-123"))
    url = _RECORD["url"]
    path = _RECORD["path"]
    assert path == "/v1internal:streamGenerateContent", f"wire path was {path!r}"
    assert _RECORD["params"].get("alt") == "sse", f"params were {_RECORD['params']}"
    assert "/v1beta" not in url, f"probe dialed /v1beta: {url}"
    assert not path.endswith("/chat/completions"), (
        f"probe dialed literal /chat/completions: {path!r}"
    )
    assert "/chat/completions" not in url, f"probe dialed /chat/completions: {url}"


def test_probe_sends_ide_envelope_and_auth():
    asyncio.run(_probe_and_close(_sse_handler, "gemini-3.6-flash-high", "tok-123"))
    h = _RECORD["headers"]
    assert h.get("user-agent") == "antigravity/hub/2.24.0 windows/amd64", h.get("user-agent")
    assert h.get("authorization") == "Bearer tok-123", h.get("authorization")
    envelope = json.loads(_RECORD["body"])
    for key in ("model", "project", "requestId", "userAgent", "requestType"):
        assert key in envelope, f"envelope missing {key!r}: {sorted(envelope)}"
    assert envelope["model"] == "gemini-3.6-flash-high", envelope.get("model")
    assert envelope["userAgent"] == "antigravity", envelope.get("userAgent")
    assert envelope["requestType"] == "agent", envelope.get("requestType")
    assert envelope["request"].get("contents"), envelope["request"]
