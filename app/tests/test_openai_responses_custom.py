"""Regression coverage for custom providers configured with format: openai-responses.

Verifies:
1. `_is_responses_provider` recognizes both 'codex' and format == 'openai-responses'.
2. Custom text upstream URL builder maps 'openai-responses' to '/v1/responses'.
3. Header injection sets originator, OpenAI-Beta, and codex UA for openai-responses format.
4. Anthropic egress normalizer routes openai-responses through responses_sse_to_openai_sse.
5. End-to-end ChatCompletion pass transforms OpenAI request to Responses body and adapts SSE.
"""
import asyncio
import json
import pytest

import app.main as main
from app.utils.url_normalization import build_custom_text_upstream_url
from app.compat.stream_normalizer import StreamNormalizer


def test_is_responses_provider_helper():
    assert main._is_responses_provider("codex") is True
    assert main._is_responses_provider("xingchi", {"format": "openai-responses"}) is True
    assert main._is_responses_provider("custom_r", {"format": "OPENAI-RESPONSES"}) is True
    assert main._is_responses_provider("openai", {"format": "openai"}) is False
    assert main._is_responses_provider("anthropic", {"format": "anthropic"}) is False


def test_custom_text_url_openai_responses():
    url = build_custom_text_upstream_url(
        "https://api.xingchiapi.com",
        "openai-responses",
        "gpt-6.1-sol",
        is_stream=True,
    )
    assert url == "https://api.xingchiapi.com/v1/responses"


def test_header_injection_openai_responses():
    headers = {}
    provider_config = {
        "type": "custom",
        "format": "openai-responses",
        "header_profile": "codex",
    }
    main._inject_provider_headers(headers, "xingchi", {}, provider_config)
    assert headers.get("originator") == "codex"
    assert headers.get("OpenAI-Beta") == "codex-1"
    assert "codex_cli_rs" in headers.get("User-Agent", "")


def test_anthropic_egress_openai_responses():
    def _sse(events):
        return b"".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            for name, data in events
        )

    async def _aiter(payload):
        yield payload

    payload = _sse([
        ("response.output_text.delta", {"delta": "hello from xingchi"}),
        ("response.completed", {
            "response": {
                "model": "gpt-6.1-sol",
                "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 15},
            }
        }),
    ])

    async def collect():
        source = main._anthropic_egress_openai_source("xingchi", _aiter(payload))
        normalizer = StreamNormalizer("openai_sse", "anthropic_sse", model_name="gpt-6.1-sol")
        return b"".join([chunk async for chunk in normalizer.convert_openai_to_anthropic(source)])

    # Mock cs_get_config to return xingchi provider configuration
    import unittest.mock as mock
    fake_cfg = {
        "providers": {
            "xingchi": {"format": "openai-responses", "type": "custom"}
        }
    }
    with mock.patch("app.main.cs_get_config", return_value=fake_cfg):
        text = asyncio.run(collect()).decode("utf-8")
        assert "content_block_delta" in text
        assert "hello from xingchi" in text
        assert '"output_tokens": 15' in text
        assert "message_stop" in text


def test_openai_to_responses_payload_conversion():
    from app import codex_adapter
    chat_payload = {
        "model": "gpt-6.1-sol",
        "messages": [
            {"role": "system", "content": "You are a helpful coding assistant."},
            {"role": "user", "content": "Solve problem X"},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
        "stream": False,
    }
    responses_payload = codex_adapter.openai_to_responses(chat_payload)
    assert responses_payload["model"] == "gpt-6.1-sol"
    assert responses_payload["store"] is False
    assert responses_payload["stream"] is True
    assert responses_payload["instructions"] == "You are a helpful coding assistant."
    assert len(responses_payload["input"]) == 1
    assert responses_payload["input"][0]["role"] == "user"
    assert responses_payload["input"][0]["content"] == "Solve problem X"
    # Unsupported OpenAI parameters are excluded
    assert "messages" not in responses_payload
    assert "temperature" not in responses_payload
    assert "max_tokens" not in responses_payload


def test_non_stream_responses_assembly_to_chat_completion():
    from app import codex_adapter

    def _sse(events):
        return b"".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            for name, data in events
        )

    async def _aiter(payload):
        yield payload

    payload = _sse([
        ("response.output_text.delta", {"delta": "Solution: 42"}),
        ("response.completed", {
            "response": {
                "id": "resp_test_123",
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            }
        }),
    ])

    async def _run():
        return await codex_adapter.assemble_responses_from_sse(_aiter(payload))

    completed = asyncio.run(_run())
    result = codex_adapter.responses_json_to_openai(completed, model="gpt-6.1-sol")
    assert result["model"] == "gpt-6.1-sol"
    assert result["choices"][0]["message"]["content"] == "Solution: 42"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 12
    assert result["usage"]["completion_tokens"] == 8
