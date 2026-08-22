"""Unit tests for Codex Responses-API adapter, Kiro profileArn injection,
Kiro refresh routing, and try_auto_import_kiro. No network calls."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import codex_adapter
from app import kiro_adapter


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_sse(events: list[tuple[str, dict]]) -> list[bytes]:
    """Build synthetic SSE byte chunks from (event_name, data) pairs."""
    chunks = []
    for name, data in events:
        chunks.append(f"event: {name}\n".encode())
        chunks.append(f"data: {json.dumps(data)}\n\n".encode())
    return chunks


async def _aiter(items):
    """Turn a sync list into a simple async iterator."""
    for item in items:
        yield item


# ── 1. Effort mapping ────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort,expected", [
    ("minimal", "low"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("max", "xhigh"),
    ("xhigh", "xhigh"),
    ("none", "none"),
])
def test_effort_mapping(effort, expected):
    body = {"reasoning_effort": effort}
    result = codex_adapter.openai_to_responses(body)
    assert result["reasoning"]["effort"] == expected


def test_effort_absent_defaults_medium():
    result = codex_adapter.openai_to_responses({})
    assert result["reasoning"]["effort"] == "medium"


def test_effort_from_nested_reasoning():
    body = {"reasoning": {"effort": "max"}}
    result = codex_adapter.openai_to_responses(body)
    assert result["reasoning"]["effort"] == "xhigh"


def test_effort_from_thinking_dict():
    body = {"thinking": {"effort": "minimal"}}
    result = codex_adapter.openai_to_responses(body)
    assert result["reasoning"]["effort"] == "low"


# ── 2. store/stream forced ───────────────────────────────────────────────────

def test_forces_store_false_stream_true():
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": False, "store": True}
    result = codex_adapter.openai_to_responses(body)
    assert result["store"] is False
    assert result["stream"] is True


# ── 3. Strips unsupported params ─────────────────────────────────────────────

def test_strips_unsupported_params():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "max_output_tokens": 200,
        "temperature": 0.7,
        "top_p": 1.0,
        "frequency_penalty": 0.1,
        "presence_penalty": 0.2,
        "stop": ["END"],
        "logprobs": True,
        "n": 2,
        "response_format": {"type": "json_object"},
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }
    result = codex_adapter.openai_to_responses(body)
    for key in (
        "max_tokens", "max_output_tokens", "temperature", "top_p",
        "frequency_penalty", "presence_penalty", "stop", "logprobs",
        "n", "response_format", "tools", "functions", "stream_options",
    ):
        assert key not in result


# ── 4. responses_json_to_openai ──────────────────────────────────────────────

def test_responses_json_to_openai():
    resp = {
        "id": "resp_abc123",
        "model": "gpt-5.5",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Hello, "},
                    {"type": "output_text", "text": "world!"},
                ],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = codex_adapter.responses_json_to_openai(resp, model="gpt-5.5")
    assert result["id"] == "resp_abc123"
    assert result["object"] == "chat.completion"
    assert result["model"] == "gpt-5.5"
    assert result["choices"][0]["message"]["content"] == "Hello, world!"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5
    assert result["usage"]["total_tokens"] == 15


# ── 5. responses_sse_to_openai_sse ───────────────────────────────────────────

def test_responses_sse_to_openai_sse():
    events = [
        ("response.output_text.delta", {"delta": "Hello", "model": "gpt-5.5"}),
        ("response.output_text.delta", {"delta": " world"}),
        ("response.completed", {
            "response": {
                "id": "resp_x",
                "model": "gpt-5.5",
                "status": "completed",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        }),
    ]

    async def _collect():
        chunks = []
        async for chunk in codex_adapter.responses_sse_to_openai_sse(_aiter(_make_sse(events))):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert len(chunks) == 4
    parsed = [json.loads(c.decode("utf-8").split("data: ", 1)[1]) for c in chunks]
    assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert parsed[1]["choices"][0]["delta"]["content"] == "Hello"
    assert parsed[2]["choices"][0]["delta"]["content"] == " world"
    assert parsed[3]["choices"][0]["finish_reason"] == "stop"
    assert parsed[3]["usage"]["prompt_tokens"] == 4
    assert parsed[3]["usage"]["completion_tokens"] == 2


def test_responses_sse_ignores_other_events():
    events = [
        ("response.created", {"id": "resp_x"}),
        ("response.output_text.delta", {"delta": "hi"}),
        ("response.output_item.added", {"item": {}}),
    ]

    async def _collect():
        chunks = []
        async for chunk in codex_adapter.responses_sse_to_openai_sse(_aiter(_make_sse(events))):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert len(chunks) == 2  # role + one content
    parsed = [json.loads(c.decode("utf-8").split("data: ", 1)[1]) for c in chunks]
    assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert parsed[1]["choices"][0]["delta"]["content"] == "hi"


# ── 6. kiro_adapter.openai_to_kiro profileArn ────────────────────────────────

def test_kiro_profile_arn_injected():
    body = {"messages": [{"role": "user", "content": "hi"}], "model": "claude-sonnet"}
    result = kiro_adapter.openai_to_kiro(body, profile_arn="arn:aws:profile:123")
    assert result["profileArn"] == "arn:aws:profile:123"
    assert "profileArn" in result


def test_kiro_profile_arn_absent_when_none():
    body = {"messages": [{"role": "user", "content": "hi"}], "model": "claude-sonnet"}
    result = kiro_adapter.openai_to_kiro(body, profile_arn=None)
    assert "profileArn" not in result


def test_kiro_profile_arn_absent_when_empty():
    result = kiro_adapter.openai_to_kiro(
        {"messages": [{"role": "user", "content": "hi"}]},
        profile_arn="",
    )
    assert "profileArn" not in result


# ── 7. Kiro refresh endpoint selection ───────────────────────────────────────

def test_kiro_refresh_social_uses_kiro_dev():
    """Social tokens (non-UUID clientId, any authMethod) → kiro.dev endpoint."""
    from app import oauth

    captured = {}

    async def fake_post(url, data, *a, **k):
        captured["url"] = url
        captured["data"] = data
        return {"accessToken": "new_at", "refreshToken": "new_rt", "expiresIn": 3600}

    async def _run():
        return await oauth._kiro_refresh_token(
            "old_rt",
            provider_data={
                "clientId": "31g4aI0YidWQusg8CZ_EzXVzLWVhc3vtMQ",
                "authMethod": "social",
            },
        )

    with patch.object(oauth, "_post_json", fake_post):
        result = asyncio.run(_run())
    assert captured["url"] == "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"
    assert captured["data"] == {"refreshToken": "old_rt"}
    assert result["access_token"] == "new_at"


def test_kiro_refresh_oidc_uses_aws():
    """external_idp + UUID clientId → AWS OIDC endpoint."""
    from app import oauth

    captured = {}

    async def fake_post(url, data, *a, **k):
        captured["url"] = url
        captured["data"] = data
        return {"access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600}

    uuid_client_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    async def _run():
        return await oauth._kiro_refresh_token(
            "old_rt",
            provider_data={
                "clientId": uuid_client_id,
                "clientSecret": "secret",
                "authMethod": "external_idp",
                "region": "us-west-2",
            },
        )

    with patch.object(oauth, "_post_json", fake_post):
        result = asyncio.run(_run())
    assert captured["url"] == "https://oidc.us-west-2.amazonaws.com/token"
    assert captured["data"]["clientId"] == uuid_client_id
    assert result["access_token"] == "new_at"


def test_kiro_refresh_builder_id_with_uuid_uses_oidc():
    """builder-id authMethod + UUID clientId → AWS OIDC endpoint."""
    from app import oauth

    captured = {}

    async def fake_post(url, data, *a, **k):
        captured["url"] = url
        captured["data"] = data
        return {"access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600}

    uuid_client_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    async def _run():
        return await oauth._kiro_refresh_token(
            "old_rt",
            provider_data={
                "clientId": uuid_client_id,
                "clientSecret": "secret",
                "authMethod": "builder-id",
            },
        )

    with patch.object(oauth, "_post_json", fake_post):
        asyncio.run(_run())
    assert captured["url"] == "https://oidc.us-east-1.amazonaws.com/token"


# ── 8. try_auto_import_kiro no-op when connections exist ─────────────────────

def test_try_auto_import_kiro_noop_when_connections_exist():
    from app import oauth

    fake_config = {
        "providers": {
            "kiro": {
                "type": "oauth",
                "connections": [{"id": "c1", "name": "Existing"}],
            }
        }
    }

    async def _run():
        return await oauth.try_auto_import_kiro()

    with patch("app.config_state.get_mutable_config", return_value=fake_config):
        result = asyncio.run(_run())

    assert result is False


def test_try_auto_import_kiro_sso_cache_unavailable():
    """When no connections exist and SSO cache is missing → fail-open, return False."""
    from app import oauth
    from fastapi import HTTPException

    fake_config = {"providers": {"kiro": {"type": "oauth", "connections": []}}}

    async def _run():
        return await oauth.try_auto_import_kiro()

    with patch("app.config_state.get_mutable_config", return_value=fake_config), \
         patch.object(oauth, "_kiro_token_from_sso_cache", side_effect=HTTPException(status_code=404, detail="no token")):
        result = asyncio.run(_run())

    assert result is False


def test_try_auto_import_kiro_imports_successfully():
    """When no connections and SSO cache has tokens → imports and returns True."""
    from app import oauth

    fake_config = {"providers": {"kiro": {"type": "oauth", "connections": []}}}
    raw_tokens = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_in": 3600,
        "profile_arn": "arn:aws:profile:123",
        "_authMethod": "social",
        "email": "user@example.com",
        "displayName": "user@example.com",
        "providerSpecificData": {"profileArn": "arn:aws:profile:123"},
    }

    async def _run():
        return await oauth.try_auto_import_kiro()

    with patch("app.config_state.get_mutable_config", return_value=fake_config), \
         patch.object(oauth, "_kiro_token_from_sso_cache", return_value=raw_tokens), \
         patch.object(oauth, "_map_kiro", return_value=raw_tokens), \
         patch.object(oauth, "_complete_connection", new=AsyncMock(return_value={"id": "c2", "email": "user@example.com"})):
        result = asyncio.run(_run())

    assert result is True
