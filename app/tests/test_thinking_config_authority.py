"""Thinking config authority + config round-trip (three-bug fix #2).

Covers:
  1. POST /api/config round-trips models[].thinking / reasoning_* / thinking_display
  2. Per-model ``thinking`` field is preferred over legacy thinking_config map
  3. Single-authority: models[].thinking feeds both Anthropic inject and
     resolve_thinking so a setting is not double-injected from divergent sources
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main
from app.compat.families import resolve_thinking
from app.compat.reasoning_policy import apply_thinking_to_anthropic_payload


def _request(path: str, body: dict) -> Request:
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("127.0.0.1", 6969),
            "client": ("127.0.0.1", 50000),
        },
        receive,
    )


def _resolve_thinking_setting(provider_config: dict, target_model: str):
    """Mirror of main.py L5499 authority: models[].thinking preferred, legacy map fallback."""
    model_entry_thinking = None
    for m in provider_config.get("models", []):
        if m.get("id") == target_model:
            model_entry_thinking = m.get("thinking")
            break
    return model_entry_thinking or provider_config.get("thinking_config", {}).get(
        target_model, "off"
    )


@pytest.fixture()
def isolated_config(monkeypatch):
    saved = cs.get_config()
    persisted = []
    monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: persisted.append(cfg))
    yield persisted
    cs.replace_config(saved)


def test_config_roundtrip_retains_thinking_fields(isolated_config):
    """POST config with thinking knobs → runtime GET retains exact values."""
    candidate = {
        "providers": {
            "kimi-test": {
                "format": "anthropic",
                "models": [
                    {
                        "id": "kimi-k2.6",
                        "enabled": True,
                        "thinking": "enabled",
                        "reasoning_mode": "pro",
                        "reasoning_context": "all_turns",
                        "thinking_display": "summarized",
                    }
                ],
            }
        },
        "admin": {"password_enabled": False, "password": "123456"},
    }
    cs.replace_config({"providers": {}, "admin": {"password_enabled": False, "password": "123456"}})

    response = asyncio.run(main.update_config(_request("/api/config", candidate)))
    assert response.status_code == 200

    after = cs.get_config()
    model = after["providers"]["kimi-test"]["models"][0]
    assert model["thinking"] == "enabled"
    assert model["reasoning_mode"] == "pro"
    assert model["reasoning_context"] == "all_turns"
    assert model["thinking_display"] == "summarized"
    assert isolated_config, "config must be persisted"


def test_thinking_authority_prefers_model_entry_over_legacy_map():
    """UI-written models[].thinking wins over legacy thinking_config map."""
    provider_config = {
        "models": [{"id": "kimi-k2.6", "thinking": "enabled"}],
        "thinking_config": {"kimi-k2.6": "off"},
    }
    assert _resolve_thinking_setting(provider_config, "kimi-k2.6") == "enabled"


def test_thinking_authority_falls_back_to_legacy_map():
    """When models[].thinking is absent, legacy map is used."""
    provider_config = {
        "models": [{"id": "kimi-k2.6"}],
        "thinking_config": {"kimi-k2.6": "high"},
    }
    assert _resolve_thinking_setting(provider_config, "kimi-k2.6") == "high"


def test_thinking_authority_default_off_when_absent():
    provider_config = {"models": [{"id": "kimi-k2.6"}]}
    assert _resolve_thinking_setting(provider_config, "kimi-k2.6") == "off"


def test_single_authority_no_divergent_double_injection():
    """Same models[].thinking value drives Anthropic inject + resolve_thinking.

    Historically L5499 read thinking_config (legacy) while L5538 read
    models[].thinking — divergent sources could inject twice. With a single
    authority both paths use the same setting and the payload has one form.
    """
    target_model = "kimi-k2.6"
    provider_name = "kimi-test"
    provider_config = {
        "format": "anthropic",
        "models": [{"id": target_model, "thinking": "enabled"}],
        # Divergent legacy value — must NOT win over models[].thinking
        "thinking_config": {target_model: "off"},
    }

    thinking_setting = _resolve_thinking_setting(provider_config, target_model)
    assert thinking_setting == "enabled"

    # Phase 3 Anthropic inject (main.py after L5499)
    payload = {"model": target_model, "messages": [], "max_tokens": 1024}
    payload = apply_thinking_to_anthropic_payload(
        payload, target_model, provider_name, thinking_setting
    )

    # Downstream single-writer path uses the same model entry (L5538 loop)
    thinking_suffix = "auto"
    for m in provider_config.get("models", []):
        if m.get("id") == target_model:
            thinking_suffix = str(m.get("thinking", "auto")).lower()
            break
    assert thinking_suffix == str(thinking_setting).lower()

    f_val = f"{provider_name}/{target_model}".lower()
    payload, _prov = resolve_thinking(
        payload,
        f_val,
        thinking_suffix,
        wire_format=provider_config.get("format", "openai"),
    )

    # Kimi binary: enable_thinking once; no extra reasoning_effort from legacy
    assert payload.get("enable_thinking") is True
    assert "reasoning_effort" not in payload
    # thinking type object must not also be present from a divergent path
    thinking_obj = payload.get("thinking")
    if isinstance(thinking_obj, dict):
        # If present it should not contradict enable_thinking (single form preferred)
        assert thinking_obj.get("type") in (None, "enabled", "adaptive")


def test_glm_single_authority_uses_model_thinking_not_legacy_off():
    """GLM-5.2 model with models[].thinking=max must not stay off via legacy map."""
    target_model = "glm-5.2-flash"
    provider_config = {
        "format": "anthropic",
        "models": [{"id": target_model, "thinking": "max"}],
        "thinking_config": {target_model: "off"},
    }
    setting = _resolve_thinking_setting(provider_config, target_model)
    assert setting == "max"

    payload = apply_thinking_to_anthropic_payload(
        {"model": target_model, "messages": []},
        target_model,
        "zhipu",
        setting,
    )
    # GLM-5.2 map: max → type=enabled + reasoning_effort=max
    assert payload.get("thinking", {}).get("type") == "enabled"
    assert payload.get("reasoning_effort") == "max"
