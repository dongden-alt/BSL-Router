"""Streamed-SSE probe engine tests: retry, TTFT deadline, idle stall (2026-10-07).

Pins the run_probe contract the gate forwards via probe_ttft_s /
probe_idle_s / probe_retries:
  - retryable failure (status 0 / >=500 / error) re-runs up to `retries`
    times with a 2s gap (gap fast-forwarded here to keep tests < 2s);
  - ttft_s is the first-chunk deadline on the streamed SSE lane;
  - idle_s is the per-chunk stall guard;
  - a non-SSE body despite stream=True falls back to JSON parsing.
All network is httpx.MockTransport — nothing leaves the process, no real
keys. asyncio.run matches the repo's sync-test convention.
"""
import asyncio
import json

import httpx

from app.authenticity.probes import run_probe


_EP = "https://router.test"
_KEY = "sk-test"          # dummy — MockTransport never sends it anywhere
_MID = "test-model"
_PROBE = {"id": "retry_ttft_probe", "prompt": "Say ready", "max_tokens": 16}


def _run(coro):
    """Run an async coroutine in a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def _sse_bytes(chunks, done=True):
    """Encode delta-content chunks as one SSE byte body."""
    lines = ["data: " + json.dumps({"choices": [{"delta": {"content": c}}]})
             for c in chunks]
    if done:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


class TestProbeRetryTtft:
    def test_retry_on_502_then_success(self, monkeypatch):
        _run(self._retry_on_502_then_success(monkeypatch))

    async def _retry_on_502_then_success(self, monkeypatch):
        calls = {"n": 0}

        # Fast-forward the engine's fixed 2s retry gap so this test stays
        # well under the per-test budget; one cooperative yield preserved.
        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *args, **kwargs):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502, json={"error": "bad gateway"})
            return httpx.Response(
                200, content=_sse_bytes(["Hello", " ", "world"]),
                headers={"content-type": "text/event-stream"},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(client, _EP, _KEY, _MID, _PROBE,
                                     ttft_s=2, idle_s=2, retries=1)
        assert calls["n"] == 2
        assert result.status_code == 200
        assert result.error == ""
        assert result.content == "Hello world"

    def test_no_retry_when_retries_zero(self):
        _run(self._no_retry_when_retries_zero())

    async def _no_retry_when_retries_zero(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(502, json={"error": "bad gateway"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(client, _EP, _KEY, _MID, _PROBE,
                                     ttft_s=2, idle_s=2, retries=0)
        assert calls["n"] == 1
        assert result.status_code == 502
        assert "HTTP 502" in result.error

    def test_first_byte_timeout(self):
        _run(self._first_byte_timeout())

    async def _first_byte_timeout(self):
        async def slow_first_byte():
            await asyncio.sleep(1.0)
            yield _sse_bytes(["late"])

        def handler(request):
            return httpx.Response(
                200, content=slow_first_byte(),
                headers={"content-type": "text/event-stream"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(client, _EP, _KEY, _MID, _PROBE,
                                     ttft_s=0.2, idle_s=5, retries=0)
        assert result.status_code == 0
        assert result.error

    def test_idle_stall_midstream(self):
        _run(self._idle_stall_midstream())

    async def _idle_stall_midstream(self):
        async def stall_after_first_chunk():
            yield b'data: {"choices": [{"delta": {"content": "one"}}]}\n\n'
            await asyncio.sleep(1.0)
            yield b"data: [DONE]\n\n"

        def handler(request):
            return httpx.Response(
                200, content=stall_after_first_chunk(),
                headers={"content-type": "text/event-stream"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(client, _EP, _KEY, _MID, _PROBE,
                                     ttft_s=5, idle_s=0.2, retries=0)
        assert result.status_code == 0
        assert result.error

    def test_non_sse_json_fallback(self):
        _run(self._non_sse_json_fallback())

    async def _non_sse_json_fallback(self):
        body = json.dumps({"choices": [{"message": {
            "content": "plain json body", "reasoning_content": ""}}]}).encode()

        def handler(request):
            return httpx.Response(
                200, content=body,
                headers={"content-type": "application/json"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(client, _EP, _KEY, _MID, _PROBE,
                                     ttft_s=2, idle_s=2, retries=0)
        assert result.status_code == 200
        assert result.content == "plain json body"
        assert result.error == ""
