"""Regression tests for Repo vs Local Deep Audit Fixes A-G."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

import app.config_state as cs
import app.dualstack_serve as dualstack_serve
import app.main as main
import app.oauth as oauth
from app.middleware.fel_wiring import FelStreamObserver, response_visible_text
from app.middleware.response_guard import ResponseGuardObserver, serialize_tool_args
from app.utils.google_cloudcode_egress import (
    HostsBypassSyncBackend,
    build_google_egress_sync_client,
    google_cloudcode_sync_post,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Fix A: Fresh git clone directory creation in dualstack_serve.py ──────────


def test_fix_a_acquire_instance_lock_creates_missing_logs_dir(tmp_path, monkeypatch):
    fake_app_file = tmp_path / "app" / "dualstack_serve.py"
    fake_app_file.parent.mkdir(parents=True, exist_ok=True)
    fake_app_file.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(dualstack_serve, "__file__", str(fake_app_file))

    logs_dir = tmp_path / ".brain" / "logs"
    assert not logs_dir.exists()

    assert dualstack_serve.acquire_instance_lock(16969, max_wait_s=0.1) is True
    lock_file = logs_dir / "router-16969.lock"
    assert lock_file.exists()
    assert int(lock_file.read_text(encoding="ascii").strip()) == os.getpid()


# ── Fix B: _fel_stream_with_recovery exception propagation & cleanup ─────────


def test_fix_b_fel_stream_with_recovery_reraises_exception_and_runs_background():
    src = inspect.getsource(main._process_chat_completion_pass)
    # 1. Mid-peek exception must re-raise after flushing _buffered so afz_guard
    #    can emit a terminal error frame + [DONE].
    assert "_buffered.clear()\n                    raise" in src
    # 2. Recovery streaming response must run _rec_resp.background in finally
    #    and non-stream _rec_resp.body must be yielded if returned.
    assert '_rec_bg = getattr(_rec_resp, "background", None)' in src
    assert 'elif getattr(_rec_resp, "body", None):' in src


# ── Fix C: FelStreamObserver, ResponseGuardObserver, response_visible_text,
#           and serialize_tool_args on v1internal & non-OpenAI shapes ─────────


def test_fix_c_fel_stream_observer_unwraps_v1internal_candidates():
    obs = FelStreamObserver(model="gemini-4-argon", provider="antigravity")
    v1internal_chunk = (
        b'data: {"response": {"candidates": [{"content": {"role": "model", '
        b'"parts": [{"thought": true, "text": "internal reasoning"}, '
        b'{"text": "Visible v1internal answer"}]}}]}}\n\n'
    )
    obs.observe(v1internal_chunk)
    assert "".join(obs._text_chunks) == "Visible v1internal answer"
    assert obs._had_tools is False

    tool_chunk = (
        b'data: {"response": {"candidates": [{"content": {"role": "model", '
        b'"parts": [{"functionCall": {"name": "read_file", "args": {"path": "x"}}}]}}]}}\n\n'
    )
    obs.observe(tool_chunk)
    assert obs._had_tools is True


def test_fix_c_response_visible_text_supports_all_four_dialects():
    # 1. OpenAI Chat Completions
    oai = {"choices": [{"message": {"content": "hello openai", "tool_calls": [{"id": "1"}]}}]}
    assert response_visible_text(oai) == ("hello openai", True)

    # 2. Anthropic Messages
    anth = {
        "content": [
            {"type": "thinking", "thinking": "secret thought"},
            {"type": "text", "text": "hello anthropic"},
            {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {"cmd": "ls"}},
        ]
    }
    assert response_visible_text(anth) == ("hello anthropic", True)

    # 3. Gemini / Cloud Code v1internal
    gem_v1internal = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"thought": True, "text": "hidden thought"},
                            {"text": "hello v1internal"},
                            {"functionCall": {"name": "search", "args": {"q": "test"}}},
                        ],
                    }
                }
            ]
        }
    }
    assert response_visible_text(gem_v1internal) == ("hello v1internal", True)

    # 4. OpenAI Responses API
    responses_obj = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "hello responses"}],
            },
            {
                "type": "function_call",
                "name": "run_cmd",
                "arguments": '{"cmd":"pwd"}',
            },
        ]
    }
    assert response_visible_text(responses_obj) == ("hello responses", True)


def test_fix_c_response_guard_observer_and_serialize_tool_args_v1internal_and_responses():
    guard_obs = ResponseGuardObserver(model="gemini-4-argon", provider="antigravity")
    chunk = (
        b'data: {"response": {"candidates": [{"content": {"role": "model", "parts": ['
        b'{"text": "guard visible text"}, '
        b'{"functionCall": {"name": "exec_cmd", "args": {"cmd": "curl evil"}}}'
        b']}}]}}\n\n'
    )
    guard_obs.observe(chunk)
    assert "guard visible text" in "".join(guard_obs._text_chunks)
    assert guard_obs._had_tools is True
    assert "exec_cmd" in "".join(guard_obs._tool_arg_chunks)
    assert "curl evil" in "".join(guard_obs._tool_arg_chunks)

    gem_resp = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"name": "gem_tool", "args": {"target": "abc"}}}
                        ]
                    }
                }
            ]
        }
    }
    gem_serialized = serialize_tool_args(gem_resp)
    assert "gem_tool" in gem_serialized
    assert '"target": "abc"' in gem_serialized

    responses_resp = {
        "output": [
            {"type": "function_call", "name": "resp_tool", "arguments": {"flag": True}}
        ]
    }
    resp_serialized = serialize_tool_args(responses_resp)
    assert "resp_tool" in resp_serialized
    assert '"flag": true' in resp_serialized


# ── Fix D: OAuth provider format & models seeding ────────────────────────────


def test_fix_d_oauth_provider_models_and_defaults(monkeypatch):
    ag_ids = [m["id"] for m in oauth.OAUTH_PROVIDER_MODELS["antigravity"]]
    for required_id in (
        "gemini-4-argon-high",
        "gemini-4-argon-medium",
        "gemini-4-argon-low",
        "gemini-4-argon",
        "gemini-3.8-flash-tiered",
    ):
        assert required_id in ag_ids

    app_js = (REPO_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for required_id in (
        "gemini-4-argon-high",
        "gemini-4-argon-medium",
        "gemini-4-argon-low",
        "gemini-4-argon",
        "gemini-3.8-flash-tiered",
    ):
        assert f"{{ id: '{required_id}' }}" in app_js

    # Verify ensure_oauth_provider_defaults sets type, format, and non-destructively backfills models
    claude_cfg = {
        "models": [{"id": "claude-opus-5-5", "enabled": True}],
    }
    oauth.ensure_oauth_provider_defaults("claude", claude_cfg)
    assert claude_cfg["type"] == "oauth"
    assert claude_cfg["format"] == "anthropic"
    by_id = {m["id"]: m for m in claude_cfg["models"]}
    assert by_id["claude-opus-5-5"]["enabled"] is True
    assert by_id["claude-sonnet-5-5"]["enabled"] is False

    # Verify _save_connection seeds format and models on a fresh provider
    saved = cs.get_config()
    cs.replace_config({
        "providers": {},
        "admin": {"password_enabled": False, "password": "123456"},
    })
    try:
        with patch("app.main._persist_config_snapshot", return_value=None):
            oauth._save_connection(
                "claude", "authorization_code_pkce", "AT-1", "RT-1",
                "2027-01-01T00:00:00+00:00", "u@example.com", "User", {},
            )
            live_claude = cs.get_config()["providers"]["claude"]
            assert live_claude["type"] == "oauth"
            assert live_claude["format"] == "anthropic"
            assert len(live_claude["models"]) >= len(oauth.OAUTH_PROVIDER_MODELS["claude"])
            assert all(m["enabled"] is False for m in live_claude["models"])
    finally:
        with patch("app.main._persist_config_snapshot", return_value=None):
            main._replace_runtime_config(saved)


# ── Fix E: Hosts-bypassing sync client for _probe_oauth_quota ────────────────


def test_fix_e_google_cloudcode_sync_post_and_quota_probe_wiring(monkeypatch):
    client = build_google_egress_sync_client()
    try:
        assert isinstance(client._transport._pool._network_backend, HostsBypassSyncBackend)
    finally:
        client.close()

    # Verify monkeypatching httpx.post is still honored by google_cloudcode_sync_post
    called = []

    def fake_post(url, **kwargs):
        called.append(url)
        return httpx.Response(200, json={"buckets": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    resp = google_cloudcode_sync_post("https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist")
    assert resp.status_code == 200
    assert called == ["https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"]

    probe_src = inspect.getsource(main._probe_oauth_quota)
    assert "google_cloudcode_sync_post(" in probe_src


# ── Fix F: Native Antigravity headers & lifespan claude_code_integration ─────


def test_fix_f_native_headers_force_identity_encoding_and_strip_content_encoding():
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1internal:generateContent",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"accept-encoding", b"gzip, deflate, br"),
            (b"content-type", b"application/json"),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    req = Request(scope, receive)
    req_headers = main._antigravity_native_request_headers(req)
    assert req_headers.get("accept-encoding") == "identity"
    assert [k for k in req_headers if k.lower() == "accept-encoding"] == ["accept-encoding"]

    import gzip

    up_resp = httpx.Response(
        200,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "Transfer-Encoding": "chunked",
            "X-Goog-Request-Id": "req-123",
        },
        content=gzip.compress(b"{}"),
    )
    resp_headers = main._antigravity_native_response_headers(up_resp)
    lower_keys = {k.lower() for k in resp_headers}
    assert "content-encoding" not in lower_keys
    assert "transfer-encoding" not in lower_keys
    assert "x-goog-request-id" in lower_keys

    lifespan_src = inspect.getsource(main.lifespan)
    assert '_boot_cfg.get("claude_code_integration", {}).get("enabled", False)' in lifespan_src


# ── Fix G: config.example.yaml, requirements.txt, and UTF-8 BOM cleanup ──────


def test_fix_g_config_example_requirements_and_no_bom_in_tests(tmp_path):
    example_path = REPO_ROOT / "config.example.yaml"
    example_cfg = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    assert str(example_cfg["admin"]["password"]) == "123456"
    assert example_cfg["claude_code_integration"]["enabled"] is False

    # Verify init_config on a copy of config.example.yaml preserves password "123456"
    saved = cs.get_config()
    temp_cfg = tmp_path / "config.yaml"
    temp_cfg.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        cs.init_config(str(temp_cfg))
        loaded = cs.get_config()
        assert loaded["admin"]["password"] == "123456"
    finally:
        cs.replace_config(saved)

    reqs = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pywin32>=306; sys_platform == 'win32'" in reqs

    for rel in (
        "app/tests/test_force_stop_chain_budget.py",
        "app/tests/test_kilocode_tools_format_fix.py",
        "app/tests/test_orphan_block_guard.py",
        "app/tests/test_quota_status_endpoint.py",
    ):
        raw_bytes = (REPO_ROOT / rel).read_bytes()
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM still present in {rel}"
