"""Regression tests for combo fallback ordering on multi-key providers.

Verifies the combo fallback contract (2026-11-25):
1. When a combo has multiple fallback models and model #1 fails (e.g. 524/503/429),
   BSL Router MUST move immediately to model #2 in the fallback list instead of
   retrying model #1 across every remaining API key of the same provider.
2. If ALL models in Pass 1 fail, Pass 2 rotates each model to its next untried
   sibling API key via `tried_conns[provider/model]`.
3. A single-model combo (no other fallback model) still rotates across sibling
   API keys immediately.
"""
from __future__ import annotations

import asyncio
import builtins
import io
import json

import httpx

import app.config_state as cs
import app.main as main


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def _openai_chunk(model: str, text: str = "ok") -> bytes:
    payload = {
        "id": f"chatcmpl-{model}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": text},
            "finish_reason": "stop",
        }],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


class _RecordingClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        payload = json.loads(request.content)
        model = payload["model"]
        auth = request.headers.get("authorization", "")
        self.calls.append((model, auth))
        return await self.handler(request, model, auth)


def _install(monkeypatch, cfg, client):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(cfg)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: None)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "req-test")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: None)


def _multi_model_same_provider_cfg():
    return {
        "settings": {"combo_infinite_retry": False},
        "error_prevention": {"enabled": False},
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "crowllm": {
                "type": "custom",
                "format": "openai",
                "connections": [
                    {"enabled": True, "api_key": "k0", "base_url": "https://crowllm.invalid/v1"},
                    {"enabled": True, "api_key": "k1", "base_url": "https://crowllm.invalid/v1"},
                    {"enabled": True, "api_key": "k2", "base_url": "https://crowllm.invalid/v1"},
                ],
                "models": [
                    {"id": "particle-1", "enabled": True, "thinking": "off"},
                    {"id": "nemotron-ultra", "enabled": True, "thinking": "off"},
                    {"id": "k2-horizon", "enabled": True, "thinking": "off"},
                ],
            },
        },
        "combos": [{
            "alias": "Uncensored Pro",
            "strategy": "fallback",
            "chain": [
                {"provider": "crowllm", "model": "particle-1"},
                {"provider": "crowllm", "model": "nemotron-ultra"},
                {"provider": "crowllm", "model": "k2-horizon"},
            ],
        }],
        "aliases": {},
    }


def test_multi_key_combo_advances_to_next_model_immediately_on_error(monkeypatch):
    """Model #1 failing with 524 on a 3-key provider must immediately try model #2,
    never retrying model #1 on k1/k2 first."""
    async def handler(request, model, auth):
        if model == "particle-1":
            return httpx.Response(524, request=request, content=b"<html>524 origin timeout</html>")
        return httpx.Response(200, request=request, stream=_ChunkStream(_openai_chunk(model, "from-nemotron")))

    client = _RecordingClient(handler)
    _install(monkeypatch, _multi_model_same_provider_cfg(), client)

    async def _run():
        resp = await main._process_chat_completion(
            {
                "model": "Uncensored Pro",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            client_wants_anthropic=False,
            client_wants_gemini=False,
        )
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(_run()).decode("utf-8", errors="replace")
    assert "from-nemotron" in body
    assert client.calls == [
        ("particle-1", "Bearer k0"),
        ("nemotron-ultra", "Bearer k0"),
    ], f"expected immediate fallback to model #2, got: {client.calls}"


def test_multi_key_combo_rotates_keys_on_second_pass_after_all_models_fail(monkeypatch):
    """When every fallback model fails on Pass 1 (k0), Pass 2 revisits model #1 on k1."""
    async def handler(request, model, auth):
        if auth == "Bearer k0":
            return httpx.Response(429, request=request, content=b'{"error":"capacity exhausted"}')
        return httpx.Response(200, request=request, stream=_ChunkStream(_openai_chunk(model, "recovered-k1")))

    client = _RecordingClient(handler)
    _install(monkeypatch, _multi_model_same_provider_cfg(), client)

    async def _run():
        resp = await main._process_chat_completion(
            {
                "model": "Uncensored Pro",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            client_wants_anthropic=False,
            client_wants_gemini=False,
        )
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(_run()).decode("utf-8", errors="replace")
    assert "recovered-k1" in body
    assert client.calls == [
        ("particle-1", "Bearer k0"),
        ("nemotron-ultra", "Bearer k0"),
        ("k2-horizon", "Bearer k0"),
        ("particle-1", "Bearer k1"),
    ], f"expected full Pass 1 across all fallback models then Pass 2 on k1, got: {client.calls}"


def test_cross_provider_multi_key_combo_advances_to_next_provider_immediately(monkeypatch):
    """Model #1 on 3-key provider A returning 429 must immediately advance to model #2 on provider B."""
    cfg = {
        "settings": {"combo_infinite_retry": False},
        "error_prevention": {"enabled": False},
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "numesai": {
                "type": "custom",
                "format": "openai",
                "connections": [
                    {"enabled": True, "api_key": "n0", "base_url": "https://numesai.invalid/v1"},
                    {"enabled": True, "api_key": "n1", "base_url": "https://numesai.invalid/v1"},
                    {"enabled": True, "api_key": "n2", "base_url": "https://numesai.invalid/v1"},
                ],
                "models": [{"id": "glm-5.3-free", "enabled": True, "thinking": "off"}],
            },
            "bslforge": {
                "type": "custom",
                "format": "openai",
                "connections": [
                    {"enabled": True, "api_key": "f0", "base_url": "https://forge.invalid/v1"},
                ],
                "models": [{"id": "zcode-glm-5.3", "enabled": True, "thinking": "off"}],
            },
        },
        "combos": [{
            "alias": "GLM-5.3",
            "strategy": "fallback",
            "chain": [
                {"provider": "numesai", "model": "glm-5.3-free"},
                {"provider": "bslforge", "model": "zcode-glm-5.3"},
            ],
        }],
        "aliases": {},
    }

    async def handler(request, model, auth):
        if model == "glm-5.3-free":
            return httpx.Response(429, request=request, content=b'{"error":"provider capacity is exhausted"}')
        return httpx.Response(200, request=request, stream=_ChunkStream(_openai_chunk(model, "from-forge")))

    client = _RecordingClient(handler)
    _install(monkeypatch, cfg, client)

    async def _run():
        resp = await main._process_chat_completion(
            {
                "model": "GLM-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            client_wants_anthropic=False,
            client_wants_gemini=False,
        )
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(_run()).decode("utf-8", errors="replace")
    assert "from-forge" in body
    assert client.calls == [
        ("glm-5.3-free", "Bearer n0"),
        ("zcode-glm-5.3", "Bearer f0"),
    ], f"expected immediate cross-provider fallback, got: {client.calls}"


def test_single_model_combo_still_rotates_sibling_keys(monkeypatch):
    """A single-model combo (no other fallback model in chain) retains sibling-key rotation."""
    cfg = {
        "settings": {"combo_infinite_retry": False},
        "error_prevention": {"enabled": False},
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "pix4k": {
                "type": "custom",
                "format": "openai",
                "connections": [
                    {"enabled": True, "api_key": "p0", "base_url": "https://pix4k.invalid/v1"},
                    {"enabled": True, "api_key": "p1", "base_url": "https://pix4k.invalid/v1"},
                ],
                "models": [{"id": "claude-opus-5.5", "enabled": True, "thinking": "off"}],
            },
        },
        "combos": [{
            "alias": "Claude-Opus-5.5",
            "strategy": "fallback",
            "chain": [
                {"provider": "pix4k", "model": "claude-opus-5.5"},
            ],
        }],
        "aliases": {},
    }

    async def handler(request, model, auth):
        if auth == "Bearer p0":
            return httpx.Response(429, request=request, content=b'{"error":"rate limited"}')
        return httpx.Response(200, request=request, stream=_ChunkStream(_openai_chunk(model, "from-p1")))

    client = _RecordingClient(handler)
    _install(monkeypatch, cfg, client)

    async def _run():
        resp = await main._process_chat_completion(
            {
                "model": "Claude-Opus-5.5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            client_wants_anthropic=False,
            client_wants_gemini=False,
        )
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(_run()).decode("utf-8", errors="replace")
    assert "from-p1" in body
    assert client.calls == [
        ("claude-opus-5.5", "Bearer p0"),
        ("claude-opus-5.5", "Bearer p1"),
    ], f"expected sibling-key rotation for single-model combo, got: {client.calls}"


def test_neon_thinking_only_session_unavailable_is_transient_and_triggers_combo_fallback(monkeypatch):
    """Crucible regression: Forge 503 session_unavailable for thinking-only / empty DOM turns
    must NOT be misclassified as terminal pool death, and must immediately fall back to model #2."""
    for transient_msg in (
        '{"error": "session_unavailable: Gemini turn settled after thinking with empty DOM response body"}',
        '{"error": "session_unavailable: bound Gemini relay returned thinking only without final answer"}',
        '{"error": "session_unavailable: bound Gemini relay returned no output"}',
    ):
        assert main._is_terminal_bslforge_pool_exhaustion(503, transient_msg) is False
    assert main._is_terminal_bslforge_pool_exhaustion(503, '{"error": "session_unavailable: browser session is signed out"}') is True

    cfg = {
        "settings": {"combo_infinite_retry": False},
        "error_prevention": {"enabled": False},
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "bslforge": {
                "type": "custom",
                "format": "openai",
                "connections": [
                    {"enabled": True, "api_key": "f0", "base_url": "https://forge.invalid/v1"},
                ],
                "models": [
                    {"id": "gemini-paid/gemini-4-neon", "enabled": True, "thinking": "high"},
                    {"id": "gemini-paid/gemini-4-argon", "enabled": True, "thinking": "high"},
                ],
            },
        },
        "combos": [{
            "alias": "Neon-With-Fallback",
            "strategy": "fallback",
            "chain": [
                {"provider": "bslforge", "model": "gemini-paid/gemini-4-neon"},
                {"provider": "bslforge", "model": "gemini-paid/gemini-4-argon"},
            ],
        }],
        "aliases": {},
    }

    async def handler(request, model, auth):
        if model == "gemini-paid/gemini-4-neon":
            return httpx.Response(
                503,
                request=request,
                content=b'{"error":{"code":"session_unavailable","message":"session_unavailable: bound Gemini relay returned thinking only without final answer"}}',
            )
        return httpx.Response(200, request=request, stream=_ChunkStream(_openai_chunk(model, "from-argon-fallback")))

    client = _RecordingClient(handler)
    _install(monkeypatch, cfg, client)

    async def _run():
        resp = await main._process_chat_completion(
            {
                "model": "Neon-With-Fallback",
                "messages": [{"role": "user", "content": "solve"}],
                "stream": True,
            },
            client_wants_anthropic=False,
            client_wants_gemini=False,
        )
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(_run()).decode("utf-8", errors="replace")
    assert "from-argon-fallback" in body
    assert client.calls == [
        ("gemini-paid/gemini-4-neon", "Bearer f0"),
        ("gemini-paid/gemini-4-argon", "Bearer f0"),
    ]
