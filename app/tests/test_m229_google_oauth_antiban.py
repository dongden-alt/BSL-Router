"""M229 Google Pro/Ultra OAuth Anti-Ban Hardening regression & contract suite."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import oauth as oauth_mod
from app import oauth_antiban
from app.circuit_breaker import CircuitBreaker, is_terminal_account_ban
from app.compat.adapters import antigravity_upstream
from app.utils.quota_live import QuotaCache


def test_unified_zero_chimera_ua_and_uuidv4_session_id():
    import app.main as main_mod

    expected_ua = "antigravity/hub/2.33.0 windows/amd64"
    assert oauth_mod.ANTIGRAVITY_UPSTREAM_UA == expected_ua
    assert antigravity_upstream.ANTIGRAVITY_UPSTREAM_UA == expected_ua
    assert main_mod._ag_upstream_contract.ANTIGRAVITY_UPSTREAM_UA == expected_ua

    cfg = oauth_mod.OAUTH_PROVIDERS["antigravity"]["config"]
    assert cfg["loadCodeAssistUserAgent"] == expected_ua
    assert "loadCodeAssistApiClient" not in cfg
    assert "loadCodeAssistClientMetadata" not in cfg

    inner = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        wrapped = antigravity_upstream.wrap_antigravity_upstream_client(
            inner,
            "gemini-3.1-pro-high",
            project="proj-alpha",
            account_id="acct-alpha",
        )
        req1 = wrapped.build_request(
            "POST",
            "https://daily-cloudcode-pa.googleapis.com/chat/completions",
            headers={"Authorization": "Bearer tok-1", "X-Session-Id": "conv-42"},
            content=json.dumps({"model": "gemini-3.1-pro-high", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
        )
        req2 = wrapped.build_request(
            "POST",
            "https://daily-cloudcode-pa.googleapis.com/chat/completions",
            headers={"Authorization": "Bearer tok-1", "X-Session-Id": "conv-42"},
            content=json.dumps({"model": "gemini-3.1-pro-high", "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "hi 2"}]}).encode("utf-8"),
        )
        assert req1.headers["user-agent"] == expected_ua
        assert "x-goog-api-client" not in {k.lower() for k in req1.headers.keys()}
        assert "client-metadata" not in {k.lower() for k in req1.headers.keys()}

        env1 = json.loads(req1.content)
        env2 = json.loads(req2.content)
        sid1 = env1["request"]["sessionId"]
        sid2 = env2["request"]["sessionId"]
        parsed_uuid = uuid.UUID(sid1)
        assert parsed_uuid.version == 4
        assert sid1 == sid2

        wrapped_other = antigravity_upstream.wrap_antigravity_upstream_client(
            inner,
            "gemini-3.1-pro-high",
            project="proj-beta",
            account_id="acct-beta",
        )
        req_other = wrapped_other.build_request(
            "POST",
            "https://daily-cloudcode-pa.googleapis.com/chat/completions",
            headers={"Authorization": "Bearer tok-2", "X-Session-Id": "conv-42"},
            content=json.dumps({"model": "gemini-3.1-pro-high", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
        )
        sid_other = json.loads(req_other.content)["request"]["sessionId"]
        assert uuid.UUID(sid_other).version == 4
        assert sid_other != sid1
    finally:
        asyncio.run(inner.aclose())


def test_google_error_classifier_and_terminal_detection():
    assert oauth_antiban.classify_google_http_error(403, '{"error":{"message":"Violation of Terms of Service"}}') == "tos_ban"
    assert oauth_antiban.classify_google_http_error(400, '{"error":"invalid_grant","error_description":"Account_suspended"}') == "tos_ban"
    assert oauth_antiban.classify_google_http_error(403, '{"error":{"status":"PERMISSION_DENIED","details":[{"reason":"VALIDATION_REQUIRED","validation_url":"https://accounts.google.com/signin/continue"}]}}') == "validation_required"
    assert oauth_antiban.classify_google_http_error(403, '{"error":{"message":"Permission denied"}}') == "permission_denied_403"
    assert oauth_antiban.classify_google_http_error(400, '{"error":{"message":"Request payload exceeds context window"}}') == "other"
    assert oauth_antiban.classify_google_http_error(429, "Resource has been exhausted") == "rate_limit"
    assert oauth_antiban.classify_google_http_error(503, "Service unavailable") == "transient"

    assert oauth_antiban.is_google_terminal_ban_or_403("antigravity", 403, "Permission denied") is True
    assert oauth_antiban.is_google_terminal_ban_or_403("gemini-cli", 403, "VALIDATION_REQUIRED") is True
    assert oauth_antiban.is_google_terminal_ban_or_403("antigravity", 400, "Invalid prompt") is False
    assert oauth_antiban.is_google_terminal_ban_or_403("claude", 403, "Permission denied") is False


def test_egress_slot_normalization_strips_credentials_and_enforces_120s_switch_guard():
    oauth_antiban.reset_all()
    slot = oauth_antiban.normalize_egress_slot("http://secretuser:supersecretpass@198.51.100.24:10001")
    assert slot == "http://198.51.100.24:10001"
    assert "secretuser" not in slot
    assert "supersecretpass" not in slot
    assert oauth_antiban.normalize_egress_slot(None) == "direct"
    assert oauth_antiban.normalize_egress_slot("") == "direct"

    clock = {"now": 1000.0}
    guard = oauth_antiban.get_egress_switch_guard()
    guard.check_switch_allowed("antigravity", slot, "acct-1", cooldown_s=120.0, monotonic=lambda: clock["now"])
    guard.record_send("antigravity", slot, "acct-1", monotonic=lambda: clock["now"])

    # Same account on same egress IP is always allowed immediately
    clock["now"] = 1005.0
    guard.check_switch_allowed("antigravity", slot, "acct-1", cooldown_s=120.0, monotonic=lambda: clock["now"])

    # Different account on same egress IP within 120s must raise EgressSwitchCooldownError
    with pytest.raises(oauth_antiban.EgressSwitchCooldownError) as exc_info:
        guard.check_switch_allowed("antigravity", slot, "acct-2", cooldown_s=120.0, monotonic=lambda: clock["now"])
    assert 114.0 <= exc_info.value.remaining_seconds <= 116.0

    # Different egress IP is allowed immediately with zero cross-account interference
    other_slot = oauth_antiban.normalize_egress_slot("http://198.51.100.25:10002")
    guard.check_switch_allowed("antigravity", other_slot, "acct-2", cooldown_s=120.0, monotonic=lambda: clock["now"])

    # After 120s dwell expires, acct-2 can use the original slot
    clock["now"] = 1126.0
    guard.check_switch_allowed("antigravity", slot, "acct-2", cooldown_s=120.0, monotonic=lambda: clock["now"])
    oauth_antiban.reset_all()


def test_apply_affinity_shared_ip_dedup_and_active_dwell_filter():
    oauth_antiban.reset_all()
    eligible = [
        {"index": 0, "conn": {"id": "acct-a", "token_type": "oauth", "proxy_url": "http://u1:p1@203.0.113.10:10001"}},
        {"index": 1, "conn": {"id": "acct-b", "token_type": "oauth", "proxy_url": "http://u2:p2@203.0.113.10:10001"}},
        {"index": 2, "conn": {"id": "acct-c", "token_type": "oauth", "proxy_url": "http://u3:p3@203.0.113.99:10002"}},
    ]
    prov_cfg = {"type": "oauth", "round_robin": True, "block_shared_ip_auto_switch": True}
    picks = [
        oauth_antiban.apply_affinity("antigravity", prov_cfg, eligible, f"sess-{i}")[0]["index"]
        for i in range(4)
    ]
    # Index 1 shares 203.0.113.10:10001 with index 0, so only indexes 0 and 2 may be chosen
    assert picks == [0, 2, 0, 2]
    oauth_antiban.reset_all()


def test_govern_post_send_preserves_google_session_pin_on_multiturn_or_403():
    oauth_antiban.reset_all()
    tok = oauth_antiban.set_request_session_key("google-multiturn-sess")
    try:
        conn = {"id": "ag-1", "token_type": "oauth"}
        oauth_antiban.get_store().bind("google-multiturn-sess", "antigravity", 0)
        oauth_antiban.govern_post_send(
            "antigravity",
            conn,
            0,
            429,
            body_text="Resource exhausted",
            has_prior_assistant_turns=True,
        )
        # Sticky pin MUST stay bound so Turn 2+ is never replayed onto a sibling Google account
        assert oauth_antiban.get_store().lookup("google-multiturn-sess", "antigravity") == 0

        res_403 = oauth_antiban.govern_post_send(
            "antigravity",
            conn,
            0,
            403,
            body_text="PERMISSION_DENIED: VALIDATION_REQUIRED",
            has_prior_assistant_turns=False,
        )
        assert res_403["retry"] is False
        assert oauth_antiban.get_store().lookup("google-multiturn-sess", "antigravity") == 0
    finally:
        oauth_antiban.reset_request_session_key(tok)
        oauth_antiban.reset_all()


def test_circuit_breaker_never_readmits_banned_or_quarantined_connections():
    cb = CircuitBreaker({"circuit_breaker": {"enabled": True, "failure_threshold": 1}})
    conns = [
        {"index": 0, "conn": {"id": "banned-0", "provider_data": {"tos_banned": True}}},
        {"index": 1, "conn": {"id": "quarantined-1", "provider_data": {"quarantined": True}}},
        {"index": 2, "conn": {"id": "healthy-2", "provider_data": {}}},
    ]
    # Open healthy-2 on transient 503 so ALL non-banned connections are OPEN
    cb.record_outcome("antigravity", "gemini-3.1-pro-high", 2, 503, error_msg="Service unavailable")
    filtered = cb.filter_healthy_connections("antigravity", "gemini-3.1-pro-high", conns)
    assert [e["index"] for e in filtered] == [2]

    # Now record a Google 403 on index 2 -> must quarantine into banned_connections and return []
    cb.record_outcome("antigravity", "gemini-3.1-pro-high", 2, 403, error_msg="Permission denied")
    assert "antigravity/2" in cb.banned_connections
    assert cb.filter_healthy_connections("antigravity", "gemini-3.1-pro-high", conns) == []
    assert is_terminal_account_ban(403, "VALIDATION_REQUIRED: verify your account") is True


def test_google_refresh_sends_clean_ua_paces_cross_account_and_marks_tos_ban(monkeypatch):
    oauth_mod._google_egress_client = None
    oauth_mod._refresh_locks.clear()
    oauth_mod._last_refresh_at.clear()
    oauth_mod._google_provider_refresh_locks.clear()
    oauth_mod._last_google_provider_refresh_at.clear()

    seen_headers = []
    seen_accounts = []

    def fake_get_egress(account_id=None, proxy_url=None):
        seen_accounts.append((account_id, proxy_url))

        def handler(req: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(req.headers))
            body_str = req.content.decode("utf-8", errors="replace")
            if "suspended-ref" in body_str:
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": "Account has been suspended for Terms of Service violation"},
                )
            return httpx.Response(200, json={"access_token": f"fresh-{account_id}", "expires_in": 3600})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(oauth_mod, "_get_google_egress_client", fake_get_egress)
    monkeypatch.setattr(oauth_mod, "_update_connection_token", lambda *a, **k: None)
    monkeypatch.setattr(oauth_mod.asyncio, "sleep", fake_sleep)

    exp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn_a = {"id": "acct-a", "api_key": "old-a", "refresh_token": "ref-a", "expires_at": exp, "proxy_url": "http://127.0.0.1:10001"}
    conn_b = {"id": "acct-b", "api_key": "old-b", "refresh_token": "suspended-ref", "expires_at": exp, "proxy_url": "http://127.0.0.1:10002"}

    from app.utils.quota_live import account_key
    expected_key_a = account_key(conn_a)
    tok_a = asyncio.run(oauth_mod.ensure_fresh_token("antigravity", conn_a, {}))
    assert tok_a == f"fresh-{expected_key_a}"
    assert seen_headers[0]["user-agent"] == "antigravity/hub/2.33.0 windows/amd64"
    assert seen_accounts[0] == (expected_key_a, "http://127.0.0.1:10001")
    # First refresh has 0s cross-account wait
    assert sleep_calls == []

    # Second distinct account triggers cross-account pacing (>= 5.0s) and marks tos_banned on suspended refresh token
    tok_b = asyncio.run(oauth_mod.ensure_fresh_token("antigravity", conn_b, {}))
    assert tok_b == "old-b"
    assert any(d >= 4.9 for d in sleep_calls)
    assert conn_b["provider_data"]["tos_banned"] is True


def test_quota_cache_and_probe_intervals_and_fallback_quarantine(monkeypatch):
    import app.main as main_mod

    cache = QuotaCache()
    assert cache.probe_interval_s("antigravity", force=False) == 180.0
    assert cache.probe_interval_s("antigravity", force=True) == 30.0
    assert cache.probe_interval_s("gemini-cli", force=False) == 180.0
    assert cache.probe_interval_s("claude", force=False) == 180.0
    assert cache.probe_interval_s("claude", force=True) == 30.0
    assert cache.probe_interval_s("openai", force=False) == 15.0
    assert cache.probe_interval_s("openai", force=True) == 10.0

    # Verify _probe_oauth_quota passes UA + proxy_url to retrieveUserQuota fallback and quarantines 403
    recorded_calls = []

    def fake_sync_post(url, *, headers=None, json=None, timeout=4.5, account_key="", proxy_url=None):
        recorded_calls.append({"url": url, "headers": dict(headers or {}), "proxy_url": proxy_url})
        if "loadCodeAssist" in url:
            return httpx.Response(200, json={"cloudaicompanionProject": {"id": "proj-gem"}})
        if "retrieveUserQuotaSummary" in url:
            return httpx.Response(200, json={})
        if "retrieveUserQuota" in url:
            return httpx.Response(403, text='{"error":{"status":"PERMISSION_DENIED","message":"VALIDATION_REQUIRED"}}')
        return httpx.Response(200, json={})

    monkeypatch.setattr("app.utils.google_cloudcode_egress.google_cloudcode_sync_post", fake_sync_post)
    monkeypatch.setattr(main_mod, "_OAUTH_PROBE_CACHE", {})
    monkeypatch.setattr(main_mod, "_ANTIGRAVITY_SUB_CACHE", {})
    cb = CircuitBreaker({"circuit_breaker": {"enabled": True}})
    monkeypatch.setattr(main_mod, "get_breaker", lambda: cb)

    conn = {
        "id": "gem-conn-1",
        "enabled": True,
        "api_key": "ya29.test-token",
        "proxy_url": "http://127.0.0.1:10005",
        "provider_data": {},
    }
    main_mod._probe_oauth_quota("gemini-cli", 0, conn, force=True)
    fb_call = [c for c in recorded_calls if c["url"].endswith(":retrieveUserQuota")][0]
    assert fb_call["headers"]["User-Agent"] == "antigravity/hub/2.33.0 windows/amd64"
    assert fb_call["proxy_url"] == "http://127.0.0.1:10005"
    assert conn["provider_data"]["quarantined"] is True
    assert main_mod._quota_blocked("gemini-cli", 0, conn, {"enabled": True}) == "quarantined"
