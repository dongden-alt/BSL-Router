"""Tests for Moonshot Kimi K3 reasoning model parameter sanitization."""

import asyncio
import builtins
import io
import json
import httpx
import pytest
import app.config_state as cs
import app.main as main


class _RecordingClient:
    def __init__(self):
        self.payload = None

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.payload = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={"id": "chatcmpl-test", "choices": [{"message": {"content": "ok"}}]},
        )


def _block_forensics(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)


def test_kimi_k3_parameter_sanitization(monkeypatch):
    client = _RecordingClient()
    _block_forensics(monkeypatch)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    cs.replace_config({
        "tools": {
            "output_thinking_squeeze": False,
            "output_intent_driven": False,
        },
        "upstream_stream_buffer": {"enabled": False},
        "providers": {
            "vsllm-gpt": {
                "type": "custom",
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://example.invalid"}],
                "models": [{
                    "id": "kimi-k3",
                    "enabled": True,
                }],
            },
        },
        "combos": [],
        "aliases": {},
    })

    # Execute _process_chat_completion with forbidden parameters
    response = asyncio.run(main._process_chat_completion({
        "model": "vsllm-gpt/kimi-k3",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0.5,
        "frequency_penalty": 0.5,
        "n": 1,
        "stream": False,
    }))

    assert response.status_code == 200
    # Forbidden parameters must be stripped from Kimi K3 payload
    assert "temperature" not in client.payload
    assert "top_p" not in client.payload
    assert "presence_penalty" not in client.payload
    assert "frequency_penalty" not in client.payload
    assert "n" not in client.payload
    assert "thinking" not in client.payload
    # Should automatically assign reasoning_effort (defaulting to max)
    assert client.payload["reasoning_effort"] == "max"
