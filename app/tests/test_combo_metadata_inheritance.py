"""Regression coverage for Combo provider metadata inheritance."""

import asyncio
import builtins
import io
import json

import httpx

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
            json={
                "id": "chatcmpl-test",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


def _block_forensics(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)


def test_combo_keeps_provider_reasoning_mode_and_context(monkeypatch):
    client = _RecordingClient()
    _block_forensics(monkeypatch)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    cs.replace_config({
        "tools": {"output_thinking_squeeze": False},
        "upstream_stream_buffer": {"enabled": False},
        "providers": {
            "openai-provider": {
                "type": "custom",
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://example.invalid"}],
                "models": [{
                    "id": "gpt-5.6-sol",
                    "enabled": True,
                    "thinking": "max",
                    "reasoning_mode": "standard",
                    "reasoning_context": "all_turns",
                }],
            },
        },
        "combos": [{
            "alias": "GPT-5.6-SOL",
            "chain": [{
                "provider": "openai-provider",
                "model": "gpt-5.6-sol",
                "thinking": "xhigh",
            }],
            "strategy": "fallback",
        }],
        "aliases": {},
    })

    response = asyncio.run(main._process_chat_completion({
        "model": "GPT-5.6-SOL",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }))

    assert response.status_code == 200
    # Combo selection owns only the per-entry effort override.
    assert client.payload["reasoning_effort"] == "xhigh"
    # Mode/context remain provider-model metadata after Combo resolution.
    assert client.payload["reasoning"] == {
        "effort": "xhigh",
        "mode": "standard",
        "context": "all_turns",
    }
