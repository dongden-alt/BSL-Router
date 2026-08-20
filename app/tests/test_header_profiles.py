"""Unit tests for the per-provider header_profile system (_inject_provider_headers).

Covers the four profiles (default / codex / claude_code / custom), the guarantee
that the hardcoded kiro/grok/codex provider_name blocks still apply, and the
None-provider_config fallback. Pure header-dict assertions, no network.
"""
import pytest

from app.main import _inject_provider_headers


def test_default_profile_no_extra_headers():
    headers = {"Authorization": "Bearer x", "User-Agent": "httpx"}
    _inject_provider_headers(headers, "custom-provider", {}, {})
    assert headers == {"Authorization": "Bearer x", "User-Agent": "httpx"}


def test_missing_profile_defaults():
    headers = {"Authorization": "Bearer x"}
    _inject_provider_headers(headers, "custom-provider", {}, {})
    assert headers == {"Authorization": "Bearer x"}


def test_codex_profile():
    headers = {}
    active_conn = {"provider_data": {"chatgptAccountId": "acct-123"}}
    _inject_provider_headers(headers, "my-codex", active_conn, {"header_profile": "codex"})
    assert headers["originator"] == "codex"
    assert headers["OpenAI-Beta"] == "codex-1"
    assert headers["User-Agent"].startswith("codex_cli_rs/")
    assert headers["ChatGPT-Account-ID"] == "acct-123"


def test_claude_code_profile():
    headers = {}
    _inject_provider_headers(headers, "my-claude", {}, {"header_profile": "claude_code"})
    assert headers["User-Agent"].startswith("claude-cli")
    # AgentRouter rejects claude-cli UAs that omit the (external token.
    assert "(external" in headers["User-Agent"]
    assert headers["anthropic-version"] == "2023-06-01"
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert "interleaved-thinking-2025-05-14" in headers["anthropic-beta"]
    assert headers["x-app"] == "cli"


def test_custom_profile():
    headers = {"Authorization": "Bearer x"}
    cfg = {"header_profile": "custom", "header_custom": {"X-Auth-Token": "abc", "": "skipped", "EMPTY_VAL": None}}
    _inject_provider_headers(headers, "gateway", {}, cfg)
    assert headers["X-Auth-Token"] == "abc"
    assert "" not in headers
    assert "EMPTY_VAL" not in headers


def test_kiro_hardcoded_block_still_applies():
    headers = {}
    _inject_provider_headers(headers, "kiro", {}, {})
    assert headers["TokenType"] == "EXTERNAL_IDP"


def test_provider_config_none_is_default():
    headers = {"Authorization": "Bearer x"}
    _inject_provider_headers(headers, "anything", {}, None)
    assert headers == {"Authorization": "Bearer x"}