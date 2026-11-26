"""Regression & contract suite for Antigravity + Claude Code OAuth anti-ban hardening."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import oauth as oauth_mod
from app import oauth_antiban
from app.circuit_breaker import CircuitBreaker, is_terminal_account_ban
from app.compat.adapters import antigravity_upstream
from app.utils.quota_live import QuotaCache


def test_antigravity_zero_chimera_strips_proxy_and_claude_headers():
    inner = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        wrapped = antigravity_upstream.wrap_antigravity_upstream_client(
            inner,
            "gemini-3.1-pro-high",
            project="proj-clean",
            account_id="acct-clean",
        )
        req = wrapped.build_request(
            "POST",
            "https://daily-cloudcode-pa.googleapis.com/chat/completions",
            headers={
                "Authorization": "Bearer ya29.test",
                "X-Goog-Api-Client": "gl-node/22.14.0",
                "Client-Metadata": "ideType=IDE_UNSPECIFIED",
                "X-Forwarded-For": "203.0.113.5",
                "X-Real-IP": "203.0.113.5",
                "Via": "1.1 proxy",
                "anthropic-beta": "oauth-2025-04-20",
                "x-claude-code-session-id": "abc123",
                "X-Stainless-Runtime": "node",
                "x-app": "cli",
            },
            content=json.dumps(
                {"model": "gemini-3.1-pro-high", "messages": [{"role": "user", "content": "hi"}]}
            ).encode("utf-8"),
        )
        lower_keys = {k.lower() for k in req.headers.keys()}
        assert req.headers["user-agent"] == "antigravity/hub/2.33.0 windows/amd64"
        for forbidden in (
            "x-goog-api-client",
            "client-metadata",
            "x-forwarded-for",
            "x-real-ip",
            "via",
            "anthropic-beta",
            "x-claude-code-session-id",
            "x-stainless-runtime",
            "x-app",
        ):
            assert forbidden not in lower_keys
    finally:
        asyncio.run(inner.aclose())


def test_google_hard_429_exhaustion_skips_retry_storm():
    oauth_antiban.reset_all()
    conn = {"id": "ag-quota", "token_type": "oauth"}
    assert oauth_antiban.is_hard_quota_exhausted_429('{"error":{"reason":"QUOTA_EXHAUSTED"}}') is True
    assert oauth_antiban.is_hard_quota_exhausted_429('{"quotaResetDelay":"3600s"}') is True
    assert oauth_antiban.is_hard_quota_exhausted_429("Resource has been exhausted (e.g. check quota).") is False

    res_hard = oauth_antiban.govern_post_send(
        "antigravity",
        conn,
        0,
        429,
        body_text='{"error":{"status":"RESOURCE_EXHAUSTED","details":[{"reason":"QUOTA_EXHAUSTED"}]}}',
    )
    assert res_hard["retry"] is False

    res_transient = oauth_antiban.govern_post_send(
        "antigravity",
        conn,
        0,
        429,
        body_text="Resource has been exhausted (e.g. check quota).",
    )
    assert res_transient["retry"] is True
    assert res_transient["delay"] > 0
    oauth_antiban.reset_all()


def test_claude_terminal_ban_classifier_and_quarantine():
    oauth_antiban.reset_all()
    assert oauth_antiban.is_claude_terminal_ban("claude", 403, '{"error":{"type":"organization_disabled"}}') is True
    assert oauth_antiban.is_claude_terminal_ban("claude-code", 401, '{"error":"token_revoked"}') is True
    assert oauth_antiban.is_claude_terminal_ban("claudecode", 400, '{"error":"invalid_grant"}') is True
    assert oauth_antiban.is_claude_terminal_ban("claude", 401, '{"error":"token expired"}') is False
    assert oauth_antiban.is_claude_terminal_ban("openai", 403, '{"error":"organization_disabled"}') is False
    assert is_terminal_account_ban(401, "oauth_token_revoked") is True

    tok = oauth_antiban.set_request_session_key("claude-sess-ban")
    try:
        conn = {"id": "cl-1", "token_type": "oauth", "provider_data": {}}
        oauth_antiban.get_store().bind("claude-sess-ban", "claude", 0)
        out = oauth_antiban.govern_post_send(
            "claude",
            conn,
            0,
            403,
            body_text='{"error":{"message":"account_suspended due to policy"}}',
            has_prior_assistant_turns=False,
        )
        assert out["retry"] is False
        assert conn["provider_data"]["quarantined"] is True
        assert conn["provider_data"]["tos_banned"] is True
        # Terminal ban must NOT release the pin to cascade onto a sibling account
        assert oauth_antiban.get_store().lookup("claude-sess-ban", "claude") == 0
    finally:
        oauth_antiban.reset_request_session_key(tok)
        oauth_antiban.reset_all()


def test_claude_mid_session_pin_preserved_on_multiturn_429():
    oauth_antiban.reset_all()
    tok = oauth_antiban.set_request_session_key("claude-multiturn")
    try:
        conn = {"id": "cl-a", "token_type": "oauth"}
        oauth_antiban.get_store().bind("claude-multiturn", "claude", 0)
        oauth_antiban.govern_post_send(
            "claude",
            conn,
            0,
            429,
            body_text="rate_limit_error",
            has_prior_assistant_turns=True,
        )
        # Active Claude conversation must stay pinned to Account 0 instead of replaying onto Account 1
        assert oauth_antiban.get_store().lookup("claude-multiturn", "claude") == 0
    finally:
        oauth_antiban.reset_request_session_key(tok)
        oauth_antiban.reset_all()


def test_claude_same_egress_cooldown_and_shared_ip_dedup():
    oauth_antiban.reset_all()
    eligible = [
        {"index": 0, "conn": {"id": "cl-a", "token_type": "oauth", "proxy_url": "http://u:p@198.51.100.10:10001"}},
        {"index": 1, "conn": {"id": "cl-b", "token_type": "oauth", "proxy_url": "http://u2:p2@198.51.100.10:10001"}},
        {"index": 2, "conn": {"id": "cl-c", "token_type": "oauth", "proxy_url": "http://u3:p3@198.51.100.20:10002"}},
    ]
    prov_cfg = {"type": "oauth", "round_robin": True, "block_shared_ip_auto_switch": True}
    picks = [
        oauth_antiban.apply_affinity("claude", prov_cfg, eligible, f"cl-sess-{i}")[0]["index"]
        for i in range(4)
    ]
    assert picks == [0, 2, 0, 2]

    cfg = {
        "tools": {"oauth_antiban": {"min_interval_seconds": 0.0, "min_switch_cooldown_seconds": 120.0}},
        "providers": {"claude": {"type": "oauth"}},
    }
    conn_a = {"id": "cl-a", "token_type": "oauth", "proxy_url": "http://198.51.100.10:10001"}
    conn_b = {"id": "cl-b", "token_type": "oauth", "proxy_url": "http://198.51.100.10:10001"}
    conn_c = {"id": "cl-c", "token_type": "oauth", "proxy_url": "http://198.51.100.20:10002"}

    asyncio.run(oauth_antiban.govern_pre_send("claude", conn_a, 0, "s1", cfg))
    # Alias `claude-code` shares canonical `claude` egress family -> must block switch on same IP
    with pytest.raises(oauth_antiban.EgressSwitchCooldownError):
        asyncio.run(oauth_antiban.govern_pre_send("claude-code", conn_b, 1, "s2", cfg))
    # Distinct proxy IP is allowed immediately
    dec_c = asyncio.run(oauth_antiban.govern_pre_send("claude", conn_c, 2, "s3", cfg))
    assert dec_c["proceed"] is True
    oauth_antiban.reset_all()


def test_claude_overage_shield():
    conn_default = {"id": "cl-1"}
    conn_allow = {"id": "cl-1", "allow_extra_usage_overage": True}
    snap_zero_5h = {"five_hour": {"remaining_pct": 0.0}, "weekly": {"remaining_pct": 60.0}}
    snap_overage = {"five_hour": {"remaining_pct": 10.0}, "overage_status": "using_overage"}
    snap_healthy = {"five_hour": {"remaining_pct": 42.0}, "weekly": {"remaining_pct": 80.0}}

    assert oauth_antiban.is_claude_overage_shield_blocked(conn_default, snap_zero_5h) is True
    assert oauth_antiban.is_claude_overage_shield_blocked(conn_default, snap_overage) is True
    assert oauth_antiban.is_claude_overage_shield_blocked(conn_default, snap_healthy) is False
    assert oauth_antiban.is_claude_overage_shield_blocked(conn_allow, snap_zero_5h) is False
    assert oauth_antiban.is_claude_overage_shield_blocked(
        conn_default, snap_zero_5h, {"allow_extra_usage_overage": True}
    ) is False


def test_claude_refresh_sends_cli_ua_paces_and_quarantines_revoked_token(monkeypatch):
    oauth_mod._refresh_locks.clear()
    oauth_mod._last_refresh_at.clear()
    oauth_mod._claude_provider_refresh_locks.clear()
    oauth_mod._last_claude_provider_refresh_at.clear()

    seen_headers = []
    seen_accounts = []

    def fake_claude_egress(account_id=None, proxy_url=None):
        seen_accounts.append((account_id, proxy_url))

        def handler(req: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(req.headers))
            body_str = req.content.decode("utf-8", errors="replace")
            if "revoked-ref" in body_str:
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": "Refresh token revoked"},
                )
            return httpx.Response(
                200,
                json={"access_token": f"cl-fresh-{account_id}", "refresh_token": "rot-ref-2", "expires_in": 3600},
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(oauth_mod, "_get_claude_egress_client", fake_claude_egress)
    monkeypatch.setattr(oauth_mod, "_update_connection_token", lambda *a, **k: None)
    monkeypatch.setattr(oauth_mod.asyncio, "sleep", fake_sleep)

    exp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn_a = {
        "id": "cl-acct-a",
        "api_key": "old-a",
        "refresh_token": "ref-a",
        "expires_at": exp,
        "proxy_url": "http://127.0.0.1:10011",
    }
    conn_b = {
        "id": "cl-acct-b",
        "api_key": "old-b",
        "refresh_token": "revoked-ref",
        "expires_at": exp,
        "proxy_url": "http://127.0.0.1:10012",
    }

    from app.utils.quota_live import account_key

    expected_key_a = account_key(conn_a)
    tok_a = asyncio.run(oauth_mod.ensure_fresh_token("claude", conn_a, {}))
    assert tok_a == f"cl-fresh-{expected_key_a}"
    assert seen_headers[0]["user-agent"] == "claude-cli/2.0.1 (external, cli)"
    assert seen_accounts[0] == (expected_key_a, "http://127.0.0.1:10011")
    assert sleep_calls == []

    # Second distinct Claude account triggers cross-account pacing (>= 5.0s) and quarantines on invalid_grant
    tok_b = asyncio.run(oauth_mod.ensure_fresh_token("claude", conn_b, {}))
    assert tok_b == "old-b"
    assert any(d >= 4.9 for d in sleep_calls)
    assert conn_b["provider_data"]["quarantined"] is True
    assert conn_b["provider_data"]["tos_banned"] is True

    # Subsequent refresh call on quarantined account is a no-op (never hammers revoked token)
    before_calls = len(seen_accounts)
    tok_b_again = asyncio.run(oauth_mod.ensure_fresh_token("claude", conn_b, {}, force=True))
    assert tok_b_again == "old-b"
    assert len(seen_accounts) == before_calls


def test_claude_quota_probe_ua_extra_usage_and_terminal_quarantine(monkeypatch):
    import app.main as main_mod

    cache = QuotaCache()
    assert cache.probe_interval_s("claude", force=False) == 180.0
    assert cache.probe_interval_s("claude", force=True) == 30.0
    assert cache.probe_interval_s("claude-code", force=False) == 180.0

    recorded = []

    def fake_get(url, headers=None, timeout=4.5):
        recorded.append({"url": url, "headers": dict(headers or {})})
        if "tok-ok" in (headers or {}).get("Authorization", ""):
            return httpx.Response(
                200,
                json={
                    "five_hour": {"utilization": 0.4, "resets_at": "2026-11-26T18:00:00Z"},
                    "extra_usage": {"is_enabled": True, "used_credits": 12.5, "monthly_limit": 100.0, "utilization": 0.125},
                },
            )
        return httpx.Response(403, text='{"error":{"type":"organization_disabled"}}')

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(main_mod, "_OAUTH_PROBE_CACHE", {})
    cb = CircuitBreaker({"circuit_breaker": {"enabled": True}})
    monkeypatch.setattr(main_mod, "get_breaker", lambda: cb)

    conn_ok = {"id": "cl-ok", "enabled": True, "api_key": "tok-ok", "provider_data": {}}
    res_ok = main_mod._probe_oauth_quota("claude", 0, conn_ok, force=True)
    assert recorded[0]["headers"]["User-Agent"] == "claude-code/2.1.216"
    assert res_ok["extra_usage"]["is_enabled"] is True
    assert res_ok["extra_usage"]["used_credits"] == 12.5

    conn_bad = {"id": "cl-bad", "enabled": True, "api_key": "tok-bad", "provider_data": {}}
    main_mod._probe_oauth_quota("claude", 1, conn_bad, force=True)
    assert conn_bad["provider_data"]["quarantined"] is True
    assert conn_bad["provider_data"]["tos_banned"] is True
    assert main_mod._quota_blocked("claude", 1, conn_bad, {"enabled": True}) == "quarantined"


def test_main_helpers_and_claude_header_sanitization_and_overage_capture(monkeypatch):
    import app.main as main_mod

    assert main_mod._request_has_prior_assistant_turns({"messages": [{"role": "user", "content": "hi"}]}) is False
    assert main_mod._request_has_prior_assistant_turns(
        {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]}
    ) is True
    assert main_mod._request_has_prior_assistant_turns(
        {"contents": [{"role": "user", "parts": []}, {"role": "model", "parts": []}]}
    ) is True
    assert main_mod._request_has_prior_assistant_turns(
        {"input": [{"role": "assistant", "content": "done"}]}
    ) is True
    assert main_mod._parse_retry_after_header({"Retry-After": "42.5"}) == 42.5
    assert main_mod._parse_retry_after_header({"retry-after": "invalid"}) is None

    hdrs = {
        "X-Goog-Api-Client": "gl-node/22",
        "Client-Metadata": "ideType=IDE",
        "X-Forwarded-For": "1.2.3.4",
    }
    main_mod._inject_provider_headers(hdrs, "claude", {"id": "cl-1"}, {"header_profile": "claude_code"})
    lower_keys = {k.lower() for k in hdrs}
    assert hdrs["User-Agent"] == "claude-cli/2.0.1 (external, cli)"
    assert "x-goog-api-client" not in lower_keys
    assert "client-metadata" not in lower_keys
    assert "x-forwarded-for" not in lower_keys

    monkeypatch.setattr(main_mod, "_OAUTH_QUOTA", {})
    monkeypatch.setattr(main_mod, "_OAUTH_PASSIVE_BY_ACCOUNT", {})
    main_mod._capture_rate_limit_headers(
        "claude",
        0,
        {
            "anthropic-ratelimit-unified-5h-utilization": "1.0",
            "anthropic-ratelimit-unified-7d_sonnet-utilization": "0.25",
            "anthropic-ratelimit-unified-overage-status": "using_overage",
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
        },
    )
    snap = main_mod._OAUTH_QUOTA["claude"][0]
    assert snap["five_hour"]["remaining_pct"] == 0.0
    assert snap["weekly_sonnet"]["remaining_pct"] == 75.0
    assert snap["overage_status"] == "using_overage"
    assert snap["representative_claim"] == "five_hour"
    assert oauth_antiban.is_claude_overage_shield_blocked({"id": "cl-1"}, snap) is True


@pytest.mark.anyio
async def test_main_pre_send_fails_closed_on_same_egress_claude_switch():
    """Verify app/main.py no longer swallows EgressSwitchCooldownError and returns 429 + Retry-After."""
    from unittest.mock import MagicMock, patch
    from app import main as bsl_main
    from app.circuit_breaker import init_breaker

    oauth_antiban.reset_all()
    test_cfg = {
        "server": {"port": 6969},
        "error_prevention": {"enabled": True, "auto_disable_threshold": 5},
        "tools": {"oauth_antiban": {"min_interval_seconds": 0.0, "min_switch_cooldown_seconds": 120.0}},
        "providers": {
            "claude": {
                "type": "oauth",
                "format": "openai",
                "base_url": "https://mock-claude.local/v1",
                "round_robin": False,
                "upstream_stream_buffer": False,
                "connections": [
                    {"name": "claude-same-ip-0", "access_token": "tok-0", "refresh_token": "ref-0", "token_type": "oauth", "enabled": True},
                    {"name": "claude-same-ip-1", "access_token": "tok-1", "refresh_token": "ref-1", "token_type": "oauth", "enabled": True},
                ],
                "models": [{"id": "claude-sonnet-5-5", "enabled": True, "connection_indexes": [0, 1]}],
            }
        },
        "combos": [],
        "aliases": {},
    }
    sent_auths = []

    async def fake_send(req: httpx.Request, **kwargs):
        sent_auths.append(req.headers.get("Authorization", ""))
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "rate limited"}}, request=req)

    mock_client = MagicMock()
    mock_client.send = fake_send
    mock_client.build_request = lambda m, u, **kw: httpx.Request(m, u, headers=kw.get("headers"), content=kw.get("content"))

    old_cfg = bsl_main.cs_get_config()
    try:
        init_breaker(test_cfg)

        async def _fake_fresh(p, c, pcfg, force=False):
            return c.get("access_token")

        with patch.object(bsl_main, "cs_get_config", return_value=test_cfg), \
             patch.object(bsl_main, "_get_client_for_proxy", return_value=mock_client), \
             patch.object(oauth_antiban, "get_client_for_account", return_value=mock_client), \
             patch.object(bsl_main, "ensure_fresh_token", side_effect=_fake_fresh), \
             patch.object(bsl_main.obs, "_persist_entry", return_value=None):
            resp = await bsl_main._process_chat_completion(
                {"model": "claude/claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                client_wants_anthropic=False,
                client_wants_gemini=False,
            )
        assert resp.status_code == 429
        payload = json.loads(resp.body.decode("utf-8"))
        assert payload["code"] == "egress_switch_cooldown"
        assert int(resp.headers["retry-after"]) >= 1
        # Account 1 MUST NOT have been sent over Account 0's shared direct IP
        assert sent_auths == ["Bearer tok-0"]
    finally:
        init_breaker(old_cfg)
        oauth_antiban.reset_all()

