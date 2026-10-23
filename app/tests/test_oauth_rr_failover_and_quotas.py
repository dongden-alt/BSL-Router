from collections import deque
from pathlib import Path
import time
from unittest.mock import MagicMock, patch

import pytest

from app import oauth_antiban
from app.circuit_breaker import _classify_for_breaker, init_breaker
from app.error_prevention import ErrorPreventionManager
from app.middleware.stream_guard import is_scaffolding_only
from app.utils.model_resolver import _PROVIDER_RR_STATE, _pick_connection, set_affinity_hook


def test_oauth_round_robin_bypasses_sticky_affinity_and_rotates():
    """When round_robin is True on an OAuth provider, new sessions rotate accounts while turns within the same session stay pinned."""
    _PROVIDER_RR_STATE.clear()
    oauth_antiban.reset_store()
    set_affinity_hook(oauth_antiban.resolver_affinity_hook)

    conns = [
        {"name": "acct-0", "access_token": "tok-0", "refresh_token": "ref-0", "token_type": "oauth", "enabled": True},
        {"name": "acct-1", "access_token": "tok-1", "refresh_token": "ref-1", "token_type": "oauth", "enabled": True},
        {"name": "acct-2", "access_token": "tok-2", "refresh_token": "ref-2", "token_type": "oauth", "enabled": True},
    ]
    prov_cfg = {
        "type": "oauth",
        "format": "openai",
        "round_robin": True,
        "connections": conns,
        "models": [{"id": "gpt-5.6-sol", "enabled": True, "connection_indexes": [0, 1, 2]}],
    }
    config = {"providers": {"codex": prov_cfg}}
    breaker = init_breaker(config)

    picked_indexes = []
    for i in range(6):
        token = oauth_antiban.set_request_session_key(f"session-{i}")
        try:
            conn, idx = _pick_connection(prov_cfg, "gpt-5.6-sol", "codex", breaker)
            assert conn is not None
            picked_indexes.append(idx)
        finally:
            oauth_antiban.reset_request_session_key(token)
    assert picked_indexes == [0, 1, 2, 0, 1, 2]

    # Repeated turns within the SAME session stay pinned to account 0 (anti-ban + cache warmth)
    token = oauth_antiban.set_request_session_key("session-0")
    try:
        same_session = [_pick_connection(prov_cfg, "gpt-5.6-sol", "codex", breaker)[1] for _ in range(4)]
        assert same_session == [0, 0, 0, 0]
    finally:
        oauth_antiban.reset_request_session_key(token)


def test_oauth_antiban_releases_sticky_affinity_on_failure_status():
    """When round_robin is False (sticky default), 401/402/403/429/5xx must release the sticky session binding."""
    oauth_antiban.reset_store()

    conns = [
        {"index": 0, "conn": {"name": "acct-0", "access_token": "tok-0", "refresh_token": "ref-0", "token_type": "oauth", "enabled": True}},
        {"index": 1, "conn": {"name": "acct-1", "access_token": "tok-1", "refresh_token": "ref-1", "token_type": "oauth", "enabled": True}},
    ]
    prov_cfg = {"type": "oauth", "round_robin": False}
    token = oauth_antiban.set_request_session_key("failover-session-99")
    try:
        sess_key = oauth_antiban.current_request_session_key()
        assert sess_key is not None

        # First call pins conn 0
        out1 = oauth_antiban.apply_affinity("claude", prov_cfg, conns, sess_key)
        assert [e["index"] for e in out1] == [0]
        assert oauth_antiban.get_store().lookup(sess_key, "claude") == 0

        # Simulate 429 rate-limit on conn 0 -> govern_post_send must release sticky binding
        oauth_antiban.govern_post_send("claude", conns[0]["conn"], 0, 429)
        assert oauth_antiban.get_store().lookup(sess_key, "claude") is None

        # Next call with only [1] eligible pins conn 1
        out2 = oauth_antiban.apply_affinity("claude", prov_cfg, [conns[1]], sess_key)
        assert [e["index"] for e in out2] == [1]
        assert oauth_antiban.get_store().lookup(sess_key, "claude") == 1
    finally:
        oauth_antiban.reset_request_session_key(token)


def test_circuit_breaker_classifies_oauth_quota_and_auth_errors_wrapped_in_400():
    """HTTP 402 and 400 invalid_request_error with quota/credit/token markers must open the breaker."""
    assert _classify_for_breaker(402, "Payment required") == "auth"
    assert _classify_for_breaker(
        400,
        '{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API."}}',
    ) == "rate_limit"
    assert _classify_for_breaker(
        400,
        '{"error":{"type":"invalid_request_error","code":"usage_limit_reached","message":"You have hit your usage limit"}}',
    ) == "rate_limit"
    assert _classify_for_breaker(
        400,
        '{"error":"invalid_grant","error_description":"Token has been expired or revoked"}',
    ) == "auth"


def test_error_prevention_record_success_clears_softban():
    """When Account 1 succeeds after Account 0 triggered a softban, record_success must clear softban."""
    cfg = {"error_prevention": {"enabled": True, "auto_disable_threshold": 5}}
    mgr = ErrorPreventionManager(cfg)
    mgr.record_error("codex", "gpt-5.6-sol", 429, "rate_limit_exceeded")
    banned, ban_type, _ = mgr.is_banned("codex", "gpt-5.6-sol")
    assert banned is True
    assert ban_type == "softban"

    # Failover to Account 1 succeeds:
    mgr.record_success("codex", "gpt-5.6-sol")
    banned_after, ban_type_after, _ = mgr.is_banned("codex", "gpt-5.6-sol")
    assert banned_after is False
    assert ban_type_after is None


def test_stream_guard_recognizes_codex_responses_scaffolding_vs_content():
    """Codex response.created and response.in_progress are scaffolding; output_text.delta is content."""
    created_chunk = b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
    in_prog_chunk = b'event: response.in_progress\ndata: {"type": "response.in_progress"}\n\n'
    delta_chunk = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hello"}\n\n'

    assert is_scaffolding_only(created_chunk) is True
    assert is_scaffolding_only(in_prog_chunk) is True
    assert is_scaffolding_only(delta_chunk) is False


def test_oauth_5h_and_weekly_quota_headers_and_probes():
    """Verify 5h and 7d quota windows from Claude headers, Codex headers, active probes, and local fallback."""
    from app.main import (
        _OAUTH_PROBE_CACHE,
        _OAUTH_QUOTA,
        _capture_rate_limit_headers,
        _probe_oauth_quota,
    )

    _OAUTH_QUOTA.clear()
    _OAUTH_PROBE_CACHE.clear()
    oauth_antiban._CONNECTION_USAGE.clear()

    # 1. Claude unified 5h & 7d headers
    future_5h = int(time.time()) + 7200
    future_7d = int(time.time()) + 259200
    _capture_rate_limit_headers(
        "claude",
        0,
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.25",
            "anthropic-ratelimit-unified-5h-reset": str(future_5h),
            "anthropic-ratelimit-unified-7d-utilization": "0.60",
            "anthropic-ratelimit-unified-7d-reset": str(future_7d),
        },
    )
    q_claude = _probe_oauth_quota("claude", 0, {"access_token": ""}, force=True)
    assert q_claude["five_hour"]["label"] == "5h"
    assert q_claude["five_hour"]["remaining_pct"] == 75.0
    assert q_claude["five_hour"]["reset_in_seconds"] > 7000
    assert q_claude["weekly"]["label"] == "7d"
    assert q_claude["weekly"]["remaining_pct"] == 40.0
    assert q_claude["weekly"]["reset_in_seconds"] > 250000

    # 2. Codex primary (5h) & secondary (7d) headers
    _capture_rate_limit_headers(
        "codex",
        1,
        {
            "x-codex-primary-used-percent": "18.0",
            "x-codex-primary-reset-after-seconds": "3600",
            "x-codex-secondary-used-percent": "45.0",
            "x-codex-secondary-reset-after-seconds": "172800",
        },
    )
    q_codex = _probe_oauth_quota("codex", 1, {"access_token": ""}, force=True)
    assert q_codex["five_hour"]["remaining_pct"] == 82.0
    assert 3500 <= q_codex["five_hour"]["reset_in_seconds"] <= 3605
    assert q_codex["weekly"]["remaining_pct"] == 55.0
    assert 172000 <= q_codex["weekly"]["reset_in_seconds"] <= 172805

    # 3. Active upstream probe for Codex wham/usage
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "rate_limit": {
            "primary_window": {"used_percent": 12.5, "reset_after_seconds": 5400},
            "secondary_window": {"used_percent": 30.0, "reset_after_seconds": 432000},
        }
    }
    with patch("httpx.get", return_value=mock_resp) as m_get:
        q_probe = _probe_oauth_quota(
            "codex",
            2,
            {"access_token": "tok-live", "provider_data": {"account_id": "acct-xyz"}},
            force=True,
        )
        assert m_get.called
        assert q_probe["source"] == "oauth_api"
        assert q_probe["five_hour"]["remaining_pct"] == 87.5
        assert q_probe["weekly"]["remaining_pct"] == 70.0

    # 4. Universal local sliding-window fallback when no upstream API or header exists
    oauth_antiban._CONNECTION_USAGE[("grok-cli", 0)] = deque([(time.monotonic() - 600, 100_000.0)])
    q_local = _probe_oauth_quota("grok-cli", 0, {"access_token": ""}, force=True)
    assert "five_hour" in q_local and "weekly" in q_local
    assert q_local["five_hour"]["remaining_pct"] == 95.0
    assert q_local["five_hour"]["reset_in_seconds"] > 17000
    assert q_local["weekly"]["remaining_pct"] > 99.0


def test_ui_static_contract_for_oauth_round_robin_and_dual_quota_bars():
    """Verify app/static/app.js renders Round Robin switch and 5h/7d quota bars with recovery timers."""
    app_js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "function _fmtQuotaReset(w)" in app_js
    assert "function _fmtSingleQuotaWindow(label, w)" in app_js
    assert "q.five_hour" in app_js and "q.weekly" in app_js
    assert "refreshQuotaBars(true)" in app_js
    assert app_js.count("toggleProviderRoundRobin(this.checked)") >= 2


@pytest.mark.anyio
async def test_oauth_multi_account_immediate_failover_end_to_end():
    """When OAuth Account 0 returns 429, _process_chat_completion immediately retries Account 1 and clears softban."""
    import httpx
    from app import main as bsl_main

    oauth_antiban.reset_store()
    _PROVIDER_RR_STATE.clear()
    set_affinity_hook(oauth_antiban.resolver_affinity_hook)

    test_cfg = {
        "server": {"port": 6969},
        "error_prevention": {"enabled": True, "auto_disable_threshold": 5},
        "providers": {
            "claude": {
                "type": "oauth",
                "format": "openai",
                "base_url": "https://mock-claude.local/v1",
                "round_robin": False,
                "upstream_stream_buffer": False,
                "connections": [
                    {"name": "claude-acct-0", "access_token": "tok-dead-0", "refresh_token": "ref-0", "token_type": "oauth", "enabled": True},
                    {"name": "claude-acct-1", "access_token": "tok-good-1", "refresh_token": "ref-1", "token_type": "oauth", "enabled": True},
                ],
                "models": [
                    {"id": "claude-sonnet-5-5", "enabled": True, "connection_indexes": [0, 1]},
                ],
            }
        },
        "combos": [],
        "aliases": {},
    }

    old_cfg = bsl_main.cs_get_config()
    sent_auth_headers = []

    async def fake_send(req: httpx.Request, **kwargs):
        import json as _json
        auth = req.headers.get("Authorization", "")
        sent_auth_headers.append(auth)
        if "tok-dead-0" in auth:
            return httpx.Response(
                429,
                json={"error": {"type": "rate_limit_error", "message": "Account 0 rate limit reached"}},
                request=req,
            )
        req_body = _json.loads(req.content.decode("utf-8")) if req.content else {}
        resp_headers = {
            "anthropic-ratelimit-unified-5h-utilization": "0.15",
            "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 3600),
            "anthropic-ratelimit-unified-7d-utilization": "0.40",
            "anthropic-ratelimit-unified-7d-reset": str(int(time.time()) + 86400),
        }
        if req_body.get("stream"):
            sse_chunk = {
                "id": "chatcmpl-ok-1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "claude-sonnet-5-5",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello from Account 1 gracefully recovered."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
            }
            sse_bytes = f"data: {_json.dumps(sse_chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")
            return httpx.Response(200, headers={**resp_headers, "content-type": "text/event-stream"}, content=sse_bytes, request=req)
        return httpx.Response(
            200,
            headers=resp_headers,
            json={
                "id": "chatcmpl-ok-1",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "claude-sonnet-5-5",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from Account 1 gracefully recovered."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
            },
            request=req,
        )

    mock_client = MagicMock()
    mock_client.send = fake_send
    mock_client.build_request = lambda method, url, **kw: httpx.Request(method, url, headers=kw.get("headers"), content=kw.get("content"))

    try:
        init_breaker(test_cfg)

        async def _fake_fresh(p, c, pcfg, force=False):
            return c.get("access_token")

        with patch.object(bsl_main, "cs_get_config", return_value=test_cfg), \
             patch.object(bsl_main, "_get_client_for_proxy", return_value=mock_client), \
             patch.object(oauth_antiban, "get_client_for_account", return_value=mock_client), \
             patch.object(bsl_main, "ensure_fresh_token", side_effect=_fake_fresh), \
             patch.object(bsl_main.obs, "_persist_entry", return_value=None):
            # Request 1: hits Account 0 (429) -> automatically fails over to Account 1 (200 OK)
            resp1 = await bsl_main._process_chat_completion(
                {"model": "claude/claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                client_wants_anthropic=False,
                client_wants_gemini=False,
            )
            assert resp1.status_code == 200
            assert sent_auth_headers == ["Bearer tok-dead-0", "Bearer tok-good-1"]

            # Request 2: Account 0 breaker is OPEN and softban was cleared on Request 1 success, so Request 2 goes straight to Account 1 (200 OK)
            resp2 = await bsl_main._process_chat_completion(
                {"model": "claude/claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi again"}], "stream": False},
                client_wants_anthropic=False,
                client_wants_gemini=False,
            )
            assert resp2.status_code == 200
            assert sent_auth_headers == ["Bearer tok-dead-0", "Bearer tok-good-1", "Bearer tok-good-1"]
    finally:
        init_breaker(old_cfg)


def test_move_up_down_connection_order_persists_through_stale_save_guard():
    """Verify Move Up / Move Down reordering is preserved by _apply_connections_stale_save_guard."""
    from app.main import _apply_connections_stale_save_guard

    old_cfg = {
        "providers": {
            "codex": {
                "connections": [
                    {"id": "conn-1", "name": "cookie-1@example.com", "api_key": "tok-1", "enabled": True},
                    {"id": "conn-2", "name": "cookie-2@example.com", "api_key": "tok-2", "enabled": True},
                ]
            }
        }
    }
    # Client swapped Cookie 2 and Cookie 1 via Move Up / Move Down
    new_cfg = {
        "providers": {
            "codex": {
                "connections": [
                    {"id": "conn-2", "name": "cookie-2@example.com", "api_key": "", "enabled": True},
                    {"id": "conn-1", "name": "cookie-1@example.com", "api_key": "tok-1", "enabled": True},
                ]
            }
        }
    }
    _apply_connections_stale_save_guard(old_cfg, new_cfg)
    merged = new_cfg["providers"]["codex"]["connections"]
    assert [c["id"] for c in merged] == ["conn-2", "conn-1"]
    assert merged[0]["api_key"] == "tok-2"  # blanked key still restored in-place


def test_oauth_quota_cache_follows_connection_identity_after_reorder():
    """Verify Cookie 1 and Cookie 2 keep their own cached quota windows when reordered."""
    from app.main import _OAUTH_PROBE_CACHE, _OAUTH_QUOTA, _probe_oauth_quota

    _OAUTH_PROBE_CACHE.clear()
    _OAUTH_QUOTA.clear()

    c1 = {"id": "cookie-1", "name": "cairn@example.com", "access_token": "tok-c1", "provider_data": {"account_id": "a1"}}
    c2 = {"id": "cookie-2", "name": "ezulike@example.com", "access_token": "tok-c2", "provider_data": {"account_id": "a2"}}

    def fake_get(url, headers=None, **kw):
        auth = (headers or {}).get("Authorization", "")
        resp = MagicMock()
        resp.status_code = 200
        if "tok-c1" in auth:
            resp.json.return_value = {"rate_limit": {"primary_window": {"used_percent": 76.0, "reset_after_seconds": 3600}}}
        else:
            resp.json.return_value = {"rate_limit": {"primary_window": {"used_percent": 4.0, "reset_after_seconds": 3600}}}
        return resp

    with patch("httpx.get", side_effect=fake_get):
        q1_before = _probe_oauth_quota("codex", 0, c1, force=True)
        q2_before = _probe_oauth_quota("codex", 1, c2, force=True)
        assert q1_before["five_hour"]["remaining_pct"] == 24.0
        assert q2_before["five_hour"]["remaining_pct"] == 96.0

        # Now swap indices (c2 is at index 0, c1 is at index 1) and read from cache (force=False)
        q_at_idx0_after_swap = _probe_oauth_quota("codex", 0, c2, force=False)
        q_at_idx1_after_swap = _probe_oauth_quota("codex", 1, c1, force=False)
        assert q_at_idx0_after_swap["five_hour"]["remaining_pct"] == 96.0
        assert q_at_idx1_after_swap["five_hour"]["remaining_pct"] == 24.0


@pytest.mark.anyio
async def test_codex_sse_assembly_handles_done_text_and_failed_event():
    """Verify assemble_responses_from_sse captures output_text.done and response.failed."""
    from app import codex_adapter

    async def _gen_done_only():
        yield (
            b'event: response.output_text.done\n'
            b'data: {"type":"response.output_text.done","text":"OK"}\n\n'
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"id":"r1","status":"completed","output":[],"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        )

    assembled = await codex_adapter.assemble_responses_from_sse(_gen_done_only())
    oai = codex_adapter.responses_json_to_openai(assembled, "gpt-6.1-sol")
    assert oai["choices"][0]["message"]["content"] == "OK"

    async def _gen_failed():
        yield (
            b'event: response.failed\n'
            b'data: {"type":"response.failed","response":{"id":"r2","status":"failed","error":{"code":"rate_limit_exceeded","message":"Usage limit reached"}}}\n\n'
        )

    failed_obj = await codex_adapter.assemble_responses_from_sse(_gen_failed())
    assert failed_obj["status"] == "failed"
    assert failed_obj["error"]["code"] == "rate_limit_exceeded"


def test_oauth_connections_card_unified_ui_and_force_refresh():
    """Verify OAuth Connections card matches Custom Provider UI with Force Refresh & Round Robin slider."""
    app_js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "forceRefreshOAuthConnections(this)" in app_js
    assert "Force Refresh" in app_js
    assert "/api/oauth/${encodeURIComponent(activeProviderId)}/force-refresh" in app_js
    assert "_swapModelConnectionIndexes" in app_js
    assert "renderGroup('google', 'Google'" in app_js
    assert "renderGroup('claude', 'Claude'" in app_js


def test_antigravity_quota_groups_google_and_claude():
    """Verify Antigravity/Gemini OAuth quota parses into separate Google and Claude groups synced with Cloud Code."""
    from app.main import _OAUTH_PROBE_CACHE, _OAUTH_QUOTA, _probe_oauth_quota

    _OAUTH_PROBE_CACHE.clear()
    _OAUTH_QUOTA.clear()

    captured_headers = {}

    def fake_post(url, headers=None, json=None, **kw):
        captured_headers.update(headers or {})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "displayName": "Weekly Limit Remaining",
                            "window": "weekly",
                            "resetTime": "2026-10-24T03:57:33Z",
                            "description": "You have used some of your weekly limit, it will fully refresh in 1 day, 9 hours.",
                            "remainingFraction": 0.8715,
                        },
                        {
                            "bucketId": "gemini-5h",
                            "displayName": "Five Hour Limit Remaining",
                            "window": "5h",
                            "resetTime": "2026-10-22T19:01:01Z",
                            "description": "You have used some of your 5-hour limit, it will fully refresh in 26 minutes.",
                            "remainingFraction": 0.83,
                        },
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {
                            "bucketId": "3p-weekly",
                            "displayName": "Weekly Limit Remaining",
                            "window": "weekly",
                            "resetTime": "2026-10-25T16:40:38Z",
                            "description": "You have used some of your weekly limit, it will fully refresh in 2 days, 22 hours.",
                            "remainingFraction": 0.56,
                        },
                        {
                            "bucketId": "3p-5h",
                            "displayName": "Five Hour Limit Remaining",
                            "window": "5h",
                            "resetTime": "2026-10-22T23:55:32Z",
                            "remainingFraction": 1,
                        },
                    ],
                },
            ]
        }
        return resp

    conn = {"id": "ag-1", "email": "blacksandlabs.contact@gmail.com", "api_key": "ya29.valid"}
    with patch("httpx.post", side_effect=fake_post):
        q = _probe_oauth_quota("antigravity", 0, conn, force=True)

    assert "antigravity/hub/" in captured_headers.get("User-Agent", "")
    assert "groups" in q
    assert q["groups"]["google"]["label"] == "Google"
    assert q["groups"]["google"]["five_hour"]["remaining_pct"] == 83.0
    assert q["groups"]["google"]["weekly"]["remaining_pct"] == 87.2
    assert q["groups"]["claude"]["label"] == "Claude"
    assert q["groups"]["claude"]["five_hour"]["remaining_pct"] == 100.0
    assert q["groups"]["claude"]["five_hour"]["reset_in_seconds"] is None  # unused 5h window shows Ready
    assert q["groups"]["claude"]["weekly"]["remaining_pct"] == 56.0


@pytest.mark.anyio
async def test_ensure_and_probe_oauth_quota_refreshes_expired_token():
    """Verify _ensure_and_probe_oauth_quota refreshes expired OAuth token before probing upstream quota."""
    from app import main as bsl_main

    conn = {
        "id": "ag-exp",
        "email": "test@example.com",
        "api_key": "ya29.expired",
        "refresh_token": "1//refresh",
        "expires_at": "2020-01-01T00:00:00+00:00",
    }
    refreshed_calls = []

    async def fake_ensure_fresh(pid, c, pcfg, force=False):
        refreshed_calls.append(pid)
        c["api_key"] = "ya29.refreshed"
        c["expires_at"] = "2099-01-01T00:00:00+00:00"
        return "ya29.refreshed"

    def fake_probe(pid, idx, c, force=False):
        return {"source": "oauth_api", "token_seen": c.get("api_key"), "five_hour": {"remaining_pct": 83.0}, "weekly": {"remaining_pct": 87.0}}

    with patch.object(bsl_main, "ensure_fresh_token", side_effect=fake_ensure_fresh), \
         patch.object(bsl_main, "_probe_oauth_quota", side_effect=fake_probe):
        res = await bsl_main._ensure_and_probe_oauth_quota("antigravity", 0, conn, {}, force=False)

    assert refreshed_calls == ["antigravity"]
    assert res["token_seen"] == "ya29.refreshed"
    assert res["expires_at"] == "2099-01-01T00:00:00+00:00"




def test_oauth_tier_identification_across_all_providers():
    """Verify _normalize_oauth_tier identifies Free/Plus/Pro Trial/Pro/Ultra/Go/K12/Pro x5/Pro x20/Pro x50/SuperGrok/etc."""
    import base64
    import json
    from app.main import _normalize_oauth_tier

    def _make_jwt(payload: dict) -> str:
        hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
        bdy = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
        return f"{hdr}.{bdy}.sig"

    # Antigravity tiers (Free, Plus, Pro Trial, Pro, Ultra)
    assert _normalize_oauth_tier("antigravity", extra={"paidTier": {"id": "g1-ultra-tier", "name": "Google AI Ultra"}, "currentTier": {"id": "free-tier"}}) == "Ultra"
    assert _normalize_oauth_tier("antigravity", extra={"paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"}, "currentTier": {"id": "free-tier"}}) == "Pro"
    assert _normalize_oauth_tier("antigravity", extra={"paidTier": {"id": "g1-pro-trial-tier", "name": "Google AI Pro Trial"}}) == "Pro Trial"
    assert _normalize_oauth_tier("antigravity", extra={"paidTier": {"id": "g1-plus-tier", "name": "Google AI Plus"}}) == "Plus"
    assert _normalize_oauth_tier("antigravity", extra={"currentTier": {"id": "free-tier", "upgradeSubscriptionType": "GOOGLE_ONE"}}) == "Free"

    # Codex tiers (Free, Go, K12, Plus, Pro, Pro x5, Pro x20, Pro x50, Team, Enterprise)
    assert _normalize_oauth_tier("codex", extra={"plan_type": "free"}) == "Free"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "go"}) == "Go"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "k12"}) == "K12"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "plus"}) == "Plus"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "pro"}) == "Pro"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "pro_5x"}) == "Pro x5"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "pro_20x"}) == "Pro x20"
    assert _normalize_oauth_tier("codex", extra={"plan_type": "pro_50x"}) == "Pro x50"
    assert _normalize_oauth_tier("codex", conn={"id_token": _make_jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}})}) == "Plus"

    # Grok CLI tiers (JWT tier 0..6 + Grok Code fallback)
    assert _normalize_oauth_tier("grok-cli", conn={"api_key": _make_jwt({"tier": 0})}) == "Free"
    assert _normalize_oauth_tier("grok-cli", conn={"api_key": _make_jwt({"tier": 1})}) == "SuperGrok"
    assert _normalize_oauth_tier("grok-cli", conn={"api_key": _make_jwt({"tier": 4})}) == "X Premium Plus"
    assert _normalize_oauth_tier("grok-cli", conn={"api_key": _make_jwt({"tier": 5})}) == "SuperGrok Heavy"
    assert _normalize_oauth_tier("grok-cli", conn={"provider_data": {"hasGrokCodeAccess": True}}) == "Grok Code"

    # Kiro, Cursor, Claude, GitHub, Kimi tiers
    assert _normalize_oauth_tier("kiro", extra={"subscriptionInfo": {"subscriptionTitle": "KIRO PRO+"}}) == "Pro+"
    assert _normalize_oauth_tier("kiro", extra={"subscriptionInfo": {"subscriptionTitle": "KIRO FREE"}}) == "Free"
    assert _normalize_oauth_tier("cursor", extra={"planInfo": {"planName": "Ultra"}}) == "Ultra"
    assert _normalize_oauth_tier("claude", extra={"tier": "default_claude_max_20x"}) == "Max 20x"
    assert _normalize_oauth_tier("claude", extra={"tier": "default_claude_max_5x"}) == "Max 5x"
    assert _normalize_oauth_tier("github", extra={"copilot_plan": "individual_pro", "access_type_sku": "copilot_pro_plus"}) == "Pro+"
    assert _normalize_oauth_tier("kimi", extra={"user": {"membership": {"level": "LEVEL_STANDARD"}}}) == "Vivace"


def test_precise_quota_probes_for_kiro_grok_cursor_github_and_kimi():
    """Verify _probe_oauth_quota uses accurate parameters, headers, and field parsers for Kiro, Grok, Cursor, GitHub, and Kimi."""
    from app.main import _OAUTH_PROBE_CACHE, _OAUTH_QUOTA, _probe_oauth_quota

    _OAUTH_PROBE_CACHE.clear()
    _OAUTH_QUOTA.clear()

    # 1. Kiro with top-level nextDateReset, freeTrialInfo, and subscriptionInfo.subscriptionTitle
    kiro_resp = MagicMock()
    kiro_resp.status_code = 200
    kiro_resp.json.return_value = {
        "nextDateReset": 1793400000,
        "subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
        "usageBreakdownList": [
            {
                "resourceType": "AGENTIC_REQUEST",
                "currentUsageWithPrecision": 125.0,
                "usageLimitWithPrecision": 500.0,
                "freeTrialInfo": {
                    "freeTrialStatus": "ACTIVE",
                    "currentUsageWithPrecision": 10.0,
                    "usageLimitWithPrecision": 50.0,
                    "freeTrialExpiry": 1793000000,
                },
            }
        ],
    }
    with patch("httpx.get", return_value=kiro_resp) as m_kiro:
        q_kiro = _probe_oauth_quota("kiro", 0, {"api_key": "kiro-tok"}, force=True)
        sent_headers = m_kiro.call_args.kwargs.get("headers") or {}
        assert "KiroIDE" in sent_headers.get("x-amz-user-agent", "")
        assert q_kiro["plan"] == "Pro"
        assert q_kiro["weekly"]["remaining_pct"] == 75.0
        assert q_kiro["five_hour"]["remaining_pct"] == 80.0
        assert q_kiro["weekly"]["reset_at"] is not None

    # 2. Grok CLI with {val: ...} boxed values, x-xai-token-auth header, and x-email / x-userid
    grok_resp = MagicMock()
    grok_resp.status_code = 200
    grok_resp.json.return_value = {
        "config": {
            "subscriptionTier": "SuperGrok",
            "monthlyLimit": {"val": 2000},
            "includedUsed": {"val": 500},
            "onDemandCap": {"val": 1000},
            "onDemandUsed": {"val": 100},
            "billingPeriodEnd": "2026-11-01T00:00:00Z",
        }
    }
    with patch("httpx.get", return_value=grok_resp) as m_grok:
        q_grok = _probe_oauth_quota(
            "grok-cli",
            0,
            {"api_key": "grok-tok", "email": "u@x.ai", "provider_data": {"userId": "uid-1"}},
            force=True,
        )
        g_headers = m_grok.call_args.kwargs.get("headers") or {}
        assert g_headers.get("x-xai-token-auth") == "xai-grok-cli"
        assert g_headers.get("x-email") == "u@x.ai"
        assert g_headers.get("x-userid") == "uid-1"
        assert q_grok["plan"] == "SuperGrok"
        assert q_grok["weekly"]["remaining_pct"] == 75.0
        assert q_grok["five_hour"]["remaining_pct"] == 90.0

    # 3. Cursor with connect-protocol-version: 1 and top-level billingCycleEnd
    cursor_resp = MagicMock()
    cursor_resp.status_code = 200
    cursor_resp.json.return_value = {
        "billingCycleEnd": 1793400000000,
        "planInfo": {"planName": "Pro"},
        "planUsage": {"limit": 500, "includedSpend": 150, "remaining": 350},
        "spendLimitUsage": {"individualLimit": 200, "individualUsed": 20},
    }
    with patch("httpx.post", return_value=cursor_resp) as m_cur:
        q_cur = _probe_oauth_quota("cursor", 0, {"api_key": "uid_123::cur-jwt"}, force=True)
        c_headers = m_cur.call_args.kwargs.get("headers") or {}
        assert c_headers.get("Authorization") == "Bearer cur-jwt"
        assert c_headers.get("connect-protocol-version") == "1"
        assert q_cur["plan"] == "Pro"
        assert q_cur["weekly"]["remaining_pct"] == 70.0
        assert q_cur["five_hour"]["remaining_pct"] == 90.0
        assert q_cur["weekly"]["reset_at"] is not None

    # 4. GitHub Copilot with copilot_internal/user quota_snapshots
    gh_resp = MagicMock()
    gh_resp.status_code = 200
    gh_resp.json.return_value = {
        "copilot_plan": "individual_pro",
        "access_type_sku": "copilot_pro",
        "quota_reset_date": "2026-11-01T00:00:00Z",
        "quota_snapshots": {
            "premium_interactions": {"entitlement": 300, "remaining": 240, "unlimited": False},
            "chat": {"entitlement": 100, "remaining": 90, "unlimited": False},
        },
    }
    with patch("httpx.get", return_value=gh_resp) as m_gh:
        q_gh = _probe_oauth_quota("github", 0, {"api_key": "ghu_123"}, force=True)
        gh_headers = m_gh.call_args.kwargs.get("headers") or {}
        assert gh_headers.get("Authorization") == "token ghu_123"
        assert q_gh["plan"] == "Pro"
        assert q_gh["weekly"]["remaining_pct"] == 80.0
        assert q_gh["five_hour"]["remaining_pct"] == 90.0

    # 5. Kimi with /coding/v1/usages usage + limits[].detail + membership level
    kimi_resp = MagicMock()
    kimi_resp.status_code = 200
    kimi_resp.json.return_value = {
        "user": {"membership": {"level": "LEVEL_ADVANCED"}},
        "usage": {"limit": "100", "used": "35", "remaining": "65", "resetTime": "2026-10-29T00:00:00Z"},
        "limits": [{"detail": {"limit": "50", "remaining": "45", "resetTime": "2026-10-23T12:00:00Z"}}],
    }
    with patch("httpx.get", return_value=kimi_resp):
        q_kimi = _probe_oauth_quota("kimi", 0, {"api_key": "kimi-tok"}, force=True)
        assert q_kimi["plan"] == "Allegro"
        assert q_kimi["weekly"]["remaining_pct"] == 65.0
        assert q_kimi["five_hour"]["remaining_pct"] == 90.0


@pytest.mark.anyio
async def test_non_oauth_combo_rotates_all_tokens_per_leaf_before_advancing_model():
    """Non-OAuth multi-token/cookie models in a combo must rotate conn[0]->conn[1] BEFORE advancing to the next model ID."""
    import httpx
    from app import main as bsl_main

    _PROVIDER_RR_STATE.clear()
    test_cfg = {
        "server": {"port": 6969},
        "error_prevention": {"enabled": True, "auto_disable_threshold": 5},
        "providers": {
            "custom-cookie": {
                "type": "custom",
                "format": "openai",
                "base_url": "https://mock-cookie.local/v1",
                "round_robin": False,
                "upstream_stream_buffer": False,
                "connections": [
                    {"name": "cookie-1", "api_key": "ck-1", "enabled": True},
                    {"name": "cookie-2", "api_key": "ck-2", "enabled": True},
                ],
                "models": [
                    {"id": "model-alpha", "enabled": True, "connection_indexes": [0, 1]},
                    {"id": "model-beta", "enabled": True, "connection_indexes": [0, 1]},
                ],
            }
        },
        "combos": [
            {
                "alias": "MultiTokenCombo",
                "strategy": "fallback",
                "chain": [
                    "custom-cookie/model-alpha",
                    "custom-cookie/model-beta",
                ],
            }
        ],
        "aliases": {},
    }

    old_cfg = bsl_main.cs_get_config()
    attempts = []

    async def fake_send(req: httpx.Request, **kwargs):
        import json as _json
        auth = req.headers.get("Authorization", "")
        req_body = _json.loads(req.content.decode("utf-8")) if req.content else {}
        m = req_body.get("model")
        attempts.append((m, auth))
        # Fail both cookies on model-alpha and cookie-1 on model-beta; succeed on model-beta + cookie-2
        if m == "model-alpha" or "ck-1" in auth:
            return httpx.Response(429, json={"error": {"message": "cookie rate limited"}}, request=req)
        if req_body.get("stream"):
            sse = (
                f'data: {{"id":"chatcmpl-beta-ck2","object":"chat.completion.chunk","model":"{m}",'
                f'"choices":[{{"index":0,"delta":{{"role":"assistant","content":"ok from beta ck-2"}},"finish_reason":"stop"}}],'
                f'"usage":{{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}}}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"}, request=req)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-beta-ck2",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": m,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok from beta ck-2"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
            request=req,
        )

    mock_client = MagicMock()
    mock_client.send = fake_send
    mock_client.build_request = lambda method, url, **kw: httpx.Request(method, url, headers=kw.get("headers"), content=kw.get("content"))

    try:
        # Disable breaker so we purely test combo per-leaf tried_conns rotation across same-provider models
        test_cfg["circuit_breaker"] = {"enabled": False}
        init_breaker(test_cfg)
        with patch.object(bsl_main, "cs_get_config", return_value=test_cfg), \
             patch.object(bsl_main, "_get_client_for_proxy", return_value=mock_client), \
             patch.object(bsl_main.obs, "_persist_entry", return_value=None):
            resp = await bsl_main._process_chat_completion(
                {"model": "MultiTokenCombo", "messages": [{"role": "user", "content": "hello"}], "stream": False},
                client_wants_anthropic=False,
                client_wants_gemini=False,
            )
            assert resp.status_code == 200
            assert attempts == [
                ("model-alpha", "Bearer ck-1"),
                ("model-alpha", "Bearer ck-2"),
                ("model-beta", "Bearer ck-1"),
                ("model-beta", "Bearer ck-2"),
            ]
    finally:
        init_breaker(old_cfg)


def test_recursive_nested_combo_expansion_and_live_removed_leaf_pruning():
    """Verify multi-level nested combos expand recursively and advance_combo_retry skips leaves removed from live config."""
    from app.routing.combo_resolver import advance_combo_retry, resolve_combo

    cfg = {
        "providers": {
            "p1": {"connections": [{"api_key": "k1", "enabled": True}], "models": [{"id": "m1", "enabled": True}]},
            "p2": {"connections": [{"api_key": "k2", "enabled": True}], "models": [{"id": "m2", "enabled": True}]},
            "p3": {"connections": [{"api_key": "k3", "enabled": True}], "models": [{"id": "m3", "enabled": True}]},
        },
        "combos": [
            {"alias": "Level3", "strategy": "fallback", "chain": ["p2/m2"]},
            {"alias": "Level2", "strategy": "fallback", "chain": ["Level3", "p3/m3"]},
            {"alias": "TopCombo", "strategy": "fallback", "chain": ["p1/m1", "Level2"]},
        ],
    }

    res = resolve_combo("TopCombo", None, cfg, {})
    assert res.matched is True
    assert [(m, p) for m, p, _ in res.active_chain] == [("m1", "p1"), ("m2", "p2"), ("m3", "p3")]

    # Simulate in-flight snapshot taken before user removed Level3 (p2/m2) from Level2 in live config
    retry_state = {"chain": list(res.active_chain), "idx": 1}
    cfg["combos"][1]["chain"] = ["p3/m3"]  # user removed Level3 while p1/m1 was hanging

    adv = advance_combo_retry(retry_state, cfg, combo_alias="TopCombo")
    assert adv.exhausted is False
    assert (adv.target_model, adv.provider_name) == ("m3", "p3")
    assert retry_state["idx"] == 2


def test_codex_openai_to_responses_preserves_and_synthesizes_prompt_cache_key():
    """Verify openai_to_responses preserves explicit prompt_cache_key and deterministically synthesizes bsl-codex-<hash>."""
    from app.codex_adapter import openai_to_responses

    # 1. Explicit client prompt_cache_key is preserved
    explicit = openai_to_responses(
        {
            "model": "gpt-6.1-sol",
            "prompt_cache_key": "client-session-abc",
            "messages": [{"role": "user", "content": "Hello Codex"}],
        }
    )
    assert explicit["prompt_cache_key"] == "client-session-abc"

    # 2. Missing prompt_cache_key synthesizes deterministic bsl-codex-<hash> from (model, instructions, first user anchor)
    turn1 = openai_to_responses(
        {
            "model": "gpt-6.1-sol",
            "messages": [
                {"role": "system", "content": "You are an architect."},
                {"role": "user", "content": "Fix the router"},
            ],
        }
    )
    turn2 = openai_to_responses(
        {
            "model": "gpt-6.1-sol",
            "messages": [
                {"role": "system", "content": "You are an architect."},
                {"role": "user", "content": "Fix the router"},
                {"role": "assistant", "content": "Sure, inspecting..."},
                {"role": "user", "content": "Now run pytest"},
            ],
        }
    )
    assert turn1["prompt_cache_key"].startswith("bsl-codex-")
    assert turn1["prompt_cache_key"] == turn2["prompt_cache_key"]


def test_circuit_breaker_honors_resets_in_seconds_and_resolver_skips_zero_quota_oauth():
    """Verify 429 usage_limit_reached with resets_in_seconds opens breaker for window duration and _pick_connection skips 0%-quota accounts."""
    from app import main as bsl_main
    from app.circuit_breaker import CircuitBreaker

    bsl_main._OAUTH_QUOTA.clear()
    bsl_main._OAUTH_PROBE_CACHE.clear()
    _PROVIDER_RR_STATE.clear()

    cb = CircuitBreaker({"enabled": True, "failure_threshold": 3, "recovery_timeout": 30})
    err_body = '{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached","plan_type":"plus","resets_in_seconds":7200}}'
    cb.record_outcome("codex", "gpt-6.1-sol", 1, 429, err_body, out_tokens=0)

    is_open, remaining = cb.is_open("codex", "gpt-6.1-sol", 1)
    assert is_open is True
    assert remaining is not None and remaining > 7000
    assert bsl_main._OAUTH_QUOTA["codex"][1]["five_hour"]["remaining_pct"] == 0.0

    # Even if breaker is not passed or is for a different model ID, _pick_connection skips conn 1 because _OAUTH_QUOTA shows 0% remaining
    prov_cfg = {
        "type": "oauth",
        "format": "openai",
        "round_robin": True,
        "connections": [
            {"name": "healthy@example.com", "access_token": "tok-0", "enabled": True},
            {"name": "exhausted@example.com", "access_token": "tok-1", "enabled": True},
        ],
        "models": [{"id": "gpt-5.6-codex", "enabled": True, "connection_indexes": [0, 1]}],
    }
    picked = [_pick_connection(prov_cfg, "gpt-5.6-codex", "codex", breaker=None)[1] for _ in range(4)]
    assert picked == [0, 0, 0, 0]

