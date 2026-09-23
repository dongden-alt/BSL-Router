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
    # External-IdP connections (authMethod set) still get the TokenType header…
    headers = {}
    _inject_provider_headers(headers, "kiro", {"provider_data": {"authMethod": "external_idp"}}, {})
    assert headers["TokenType"] == "EXTERNAL_IDP"
    # …auth_method at the connection root works too
    headers = {}
    _inject_provider_headers(headers, "kiro", {"auth_method": "external_idp"}, {})
    assert headers["TokenType"] == "EXTERNAL_IDP"
    # …but social/Builder-ID tokens must NOT get it — the gateway 403s them
    # (empirically verified against live kiro gateway 2026-08-25).
    headers = {}
    _inject_provider_headers(headers, "kiro", {}, {})
    assert "TokenType" not in headers


def test_provider_config_none_is_default():
    headers = {"Authorization": "Bearer x"}
    _inject_provider_headers(headers, "anything", {}, None)
    assert headers == {"Authorization": "Bearer x"}


def test_opencode_prefix_gets_zen_identity_headers():
    # opencode.ai/zen 400s "MissingSessionID" without x-opencode-session
    # (observed live 2026-09-11, mimo-v2.5-free free tier).
    headers = {}
    _inject_provider_headers(headers, "opencode-zen", {"api_key": "zen-key-1"}, {})
    assert headers["x-opencode-session"].startswith("ses_")
    assert headers["x-opencode-request"].startswith("usr_")
    assert headers["x-opencode-client"] == "cli"
    assert headers["User-Agent"].startswith("opencode/")


def test_opencode_profile_for_custom_providers():
    headers = {}
    _inject_provider_headers(headers, "my-gateway", {}, {"header_profile": "opencode"})
    assert headers["x-opencode-session"].startswith("ses_")
    assert headers["x-opencode-request"].startswith("usr_")
    assert headers["x-opencode-client"] == "cli"
    assert headers["User-Agent"].startswith("opencode/")


def test_opencode_identity_stable_and_idempotent():
    # uuid5-derived ids: stable across requests (long-lived session look)
    # AND identical on double injection (hardcoded prefix + profile both
    # firing, or 401-retry re-injection).
    conn = {"api_key": "zen-key-1", "name": "conn-003"}
    h1, h2 = {}, {}
    _inject_provider_headers(h1, "opencode-zen", conn, {"header_profile": "opencode"})
    _inject_provider_headers(h2, "opencode-zen", conn, {})
    assert h1 == h2


def test_opencode_identity_differs_per_connection():
    # Round-robin across N connections must present as N distinct sessions.
    h1, h2 = {}, {}
    _inject_provider_headers(h1, "opencode-zen", {"api_key": "key-a"}, {})
    _inject_provider_headers(h2, "opencode-zen", {"api_key": "key-b"}, {})
    assert h1["x-opencode-request"] != h2["x-opencode-request"]
    assert h1["x-opencode-session"] != h2["x-opencode-session"]


def test_non_opencode_provider_gets_no_zen_headers():
    headers = {"Authorization": "Bearer x"}
    _inject_provider_headers(headers, "agentrouter-o", {}, {})
    assert "x-opencode-session" not in headers
    assert "x-opencode-client" not in headers
    assert headers == {"Authorization": "Bearer x"}


# ── Retention headers (per-provider, config-driven) ──────────────────────
# Optional dict[str,str] of zero-retention headers for upstreams that offer
# such a mechanism. Fail-open: malformed config never raises, never blocks.


def test_retention_headers_injected_as_is():
    # (a) a well-formed dict is injected verbatim (values str-cast).
    headers = {"Authorization": "Bearer x"}
    cfg = {"retention_headers": {"X-No-Retention": "true", "X-Train-Opt-Out": "1"}}
    _inject_provider_headers(headers, "custom-gateway", {}, cfg)
    assert headers["X-No-Retention"] == "true"
    assert headers["X-Train-Opt-Out"] == "1"
    # pre-existing headers preserved
    assert headers["Authorization"] == "Bearer x"


def test_retention_headers_absent_no_change():
    # (b) absent key -> no change to headers.
    headers = {"Authorization": "Bearer x"}
    _inject_provider_headers(headers, "custom-gateway", {}, {"header_profile": "default"})
    assert headers == {"Authorization": "Bearer x"}
    # explicit None behaves identically to absent
    headers2 = {"Authorization": "Bearer x"}
    _inject_provider_headers(headers2, "custom-gateway", {}, {"retention_headers": None})
    assert headers2 == {"Authorization": "Bearer x"}


def test_retention_headers_malformed_ignored_no_raise():
    # (c) non-dict values are silently ignored, never raise.
    headers = {"Authorization": "Bearer x"}
    for bad in ("not-a-dict", 42, ["X-No-Retention", "true"], True):
        _inject_provider_headers(headers, "custom-gateway", {}, {"retention_headers": bad})
    # nothing injected, no exception, auth preserved
    assert headers == {"Authorization": "Bearer x"}
    # dict containing a non-str key / None value / empty value is skipped
    headers2 = {"Authorization": "Bearer x"}
    _inject_provider_headers(
        headers2, "custom-gateway", {},
        {"retention_headers": {"": "skip-empty-key", "X-Keep": None, "X-Empty": "", "X-Ok": "1"}},
    )
    assert headers2 == {"Authorization": "Bearer x", "X-Ok": "1"}


def test_retention_headers_str_cast_non_string_values():
    # (d) non-string values are str-cast; empty-after-cast values skipped.
    headers = {"Authorization": "Bearer x"}
    _inject_provider_headers(
        headers, "custom-gateway", {},
        {"retention_headers": {"X-Count": 0, "X-Flag": True, "X-Pi": 3.14, "X-Empty": ""}},
    )
    assert headers["X-Count"] == "0"
    assert headers["X-Flag"] == "True"
    assert headers["X-Pi"] == "3.14"
    assert "X-Empty" not in headers
