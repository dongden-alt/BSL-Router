"""Regression: _RECOVERABLE must include client/validation 4xx so chains never stop early."""

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


# Every status that must trigger combo/chain advance (never terminal stop).
EXPECTED_RECOVERABLE = {
    400, 401, 403, 404, 405, 408, 409, 413, 422, 429,
    500, 502, 503, 504, 524, 525, 526,
}


def test_recoverable_includes_expanded_4xx_and_5xx():
    assert EXPECTED_RECOVERABLE.issubset(main._RECOVERABLE)
    assert main._RECOVERABLE == EXPECTED_RECOVERABLE


def test_previously_terminal_4xx_are_now_recoverable():
    """400/401/405/409/413/422 used to terminate the chain; they must advance now."""
    for code in (400, 401, 405, 409, 413, 422):
        assert code in main._RECOVERABLE, f"{code} must be recoverable"


def test_error_reports_module_attr_exists():
    """BUG1: /api/observability/artifacts reads obs.error_reports at module level."""
    import app.observability as obs

    assert hasattr(obs, "error_reports")
    assert isinstance(obs.error_reports, list)


def test_get_artifacts_defensive_getattr():
    """get_artifacts must not AttributeError if error_reports is missing."""
    import inspect

    src = inspect.getsource(main.get_artifacts)
    assert 'getattr(obs, "error_reports", [])' in src


def test_antigravity_slots_include_gemini_36():
    for slot in (
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
    ):
        assert slot in main.ANTIGRAVITY_INTEGRATION_SLOTS
    # Order: 3.6 entries prepend before 3.5
    slots = main.ANTIGRAVITY_INTEGRATION_SLOTS
    assert slots.index("gemini-3.6-flash-high") < slots.index("gemini-3.5-flash-medium")


# ── Behavioral: combo chain advances on recoverable 400 ──────────────────────


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _ScriptedClient:
    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.models = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        payload = json.loads(request.content)
        model = payload["model"]
        self.models.append(model)
        behavior = self.behaviors[model]
        if callable(behavior):
            return await behavior(request)
        return httpx.Response(200, request=request, stream=behavior)


def _openai_chunk(model: str, text: str = "", finish_reason=None) -> bytes:
    payload = {
        "id": f"chatcmpl-{model}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": text} if text else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


def _success_stream(model: str, text: str = "fallback-ok") -> _ChunkStream:
    return _ChunkStream(_openai_chunk(model, text, "stop"))


def _config_two_leaf():
    providers = {}
    for provider, model in (("dead", "dead-model"), ("healthy", "healthy-model")):
        providers[provider] = {
            "type": "custom",
            "format": "openai",
            "connections": [{
                "enabled": True,
                "api_key": "test",
                "base_url": f"https://{provider}.invalid",
            }],
            "models": [{"id": model, "enabled": True, "thinking": "off"}],
        }
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": providers,
        "combos": [{
            "alias": "GLM-5.2",
            "strategy": "fallback",
            "chain": [
                {"provider": "dead", "model": "dead-model"},
                {"provider": "healthy", "model": "healthy-model"},
            ],
        }],
        "aliases": {},
    }


def _config_single_leaf():
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": {
            "dead": {
                "type": "custom",
                "format": "openai",
                "connections": [{
                    "enabled": True,
                    "api_key": "test",
                    "base_url": "https://dead.invalid",
                }],
                "models": [{"id": "dead-model", "enabled": True, "thinking": "off"}],
            },
        },
        "combos": [{
            "alias": "GLM-5.2",
            "strategy": "fallback",
            "chain": [
                {"provider": "dead", "model": "dead-model"},
            ],
        }],
        "aliases": {},
    }


def _install(monkeypatch, client, config):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    starts = []
    ends = []
    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(config)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: None)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: starts.append(kwargs) or f"req-{len(starts)}")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: ends.append(kwargs))
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_TIMEOUT", 0.03)
    monkeypatch.setattr(main, "GEMINI_EGRESS_BODY_STALL_TIMEOUT", 0.01)
    return starts, ends


async def _collect(client_wants_gemini=True):
    response = await main._process_chat_completion(
        {
            "model": "GLM-5.2",
            "_bsl_original_model": "gemini-pro-agent",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
        client_wants_gemini=client_wants_gemini,
    )
    return [chunk async for chunk in response.body_iterator]


def test_recoverable_400_advances_combo_chain(monkeypatch):
    """Leaf0 returns 400 JSONResponse path; leaf1 success stream — chain advances once."""

    async def upstream_400(request):
        return httpx.Response(
            400,
            request=request,
            content=json.dumps({"error": {"message": "bad request", "type": "invalid_request"}}).encode(),
            headers={"content-type": "application/json"},
        )

    client = _ScriptedClient({
        "dead-model": upstream_400,
        "healthy-model": _success_stream("healthy-model", "fallback-ok"),
    })
    _install(monkeypatch, client, _config_two_leaf())

    output = b"".join(asyncio.run(_collect()))

    assert client.models == ["dead-model", "healthy-model"]
    assert b"fallback-ok" in output


def test_recoverable_400_single_leaf_surfaces_terminal(monkeypatch):
    """Single-leaf chain returning 400 must surface the error (no infinite retry).

    Continuous-fallback (2026-08-22): a 1-entry combo now receives a guaranteed
    second pass, so the leaf is attempted exactly twice (bounded, not infinite)
    before the terminal error is surfaced to the client.
    """

    async def upstream_400(request):
        return httpx.Response(
            400,
            request=request,
            content=json.dumps({"error": {"message": "bad request", "type": "invalid_request"}}).encode(),
            headers={"content-type": "application/json"},
        )

    client = _ScriptedClient({
        "dead-model": upstream_400,
    })
    _install(monkeypatch, client, _config_single_leaf())

    output = b"".join(asyncio.run(_collect()))

    # Exactly two bounded attempts (second pass of the single leaf) — not
    # infinite, and not a silent single-attempt stop.
    assert client.models == ["dead-model", "dead-model"]
    # Terminal error is surfaced to the client (status embedded in SSE error frame).
    assert b"400" in output or b"bad request" in output or b"error" in output
