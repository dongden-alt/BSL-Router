"""Quota live contract tests: authoritative values, failure truth, monotonicity, reorder safety, and native IDE wire passthrough."""
import asyncio
import json
import math
import time

import pytest
from starlette.requests import Request


def _make_request(method: str, path: str, headers: dict, body: bytes = b"{}") -> Request:
    raw_headers = [(k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in headers.items()]
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": method, "path": path, "headers": raw_headers, "query_string": b""},
        receive,
    )


@pytest.mark.parametrize("pct", [0.0, 100.0])
def test_authoritative_zero_and_full_are_preserved(pct):
    from app.main import _merge_oauth_windows

    row = _merge_oauth_windows("antigravity", 0, {"five_hour": {"label": "5h", "remaining_pct": pct}})
    assert row["five_hour"]["remaining_pct"] == pct
    assert row["remaining_pct"] == pct


def test_empty_groups_do_not_inherit_other_group():
    from app.main import _merge_oauth_windows

    row = _merge_oauth_windows(
        "antigravity",
        0,
        {
            "groups": {
                "google": {"label": "Google", "five_hour": {"label": "5h", "remaining_pct": 42.0}},
                "claude": {"label": "Claude"},
            }
        },
    )
    assert "google" in row["groups"]
    assert "claude" not in row["groups"]


def test_malformed_fraction_is_not_authoritative():
    from app.main import _merge_oauth_windows

    row = _merge_oauth_windows("antigravity", 0, {"five_hour": {"label": "5h", "remaining_pct": math.nan}})
    assert row.get("remaining_pct") is None
    assert row["source"] == "unavailable"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_failure_preserves_last_good_as_stale(status):
    from app.main import _quota_failure

    good = {"remaining_pct": 33.0, "observed_at": time.time() - 30, "source": "oauth_api"}
    row = _quota_failure(good, f"http_{status}", status, {"access_token": "secret-token-canary", "expires_at": None})
    assert row["remaining_pct"] == 33.0
    assert row["stale"] is True
    assert row["status"] == status
    assert "secret-token-canary" not in json.dumps(row)
    assert row["credential_present"] is True


def test_disabled_connection_does_not_probe(monkeypatch):
    from app.main import get_quota_status_endpoint
    from app.utils.quota_live import QuotaCache

    calls = {"n": 0}

    async def bad(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("disabled connection was probed")

    monkeypatch.setattr("app.main._QUOTA_LIVE_CACHE", QuotaCache())
    monkeypatch.setattr("app.main._ensure_and_probe_oauth_quota", bad)
    cfg = {
        "providers": {
            "antigravity": {
                "type": "oauth",
                "connections": [{"id": "a", "enabled": False, "access_token": "x"}],
            }
        }
    }
    monkeypatch.setattr("app.main.cs_get_config", lambda: cfg)
    payload = json.loads(asyncio.run(get_quota_status_endpoint(provider="antigravity")).body)
    assert payload["providers"]["antigravity"][0]["error"] == "disabled"
    assert payload["providers"]["antigravity"][0]["remaining_pct"] is None
    assert calls["n"] == 0


def test_cold_start_endpoint_returns_unknown_not_hundred(monkeypatch):
    from app.main import get_quota_status_endpoint
    from app.utils.quota_live import QuotaCache

    async def scenario():
        cache = QuotaCache()
        monkeypatch.setattr("app.main._QUOTA_LIVE_CACHE", cache)
        monkeypatch.setattr("app.main._OAUTH_PASSIVE_BY_ACCOUNT", {})
        monkeypatch.setattr("app.main._OAUTH_QUOTA", {})

        async def slow_probe(*args, **kwargs):
            await asyncio.sleep(0.05)
            return {"remaining_pct": 61.5, "source": "oauth_api", "observed_at": time.time()}

        monkeypatch.setattr("app.main._ensure_and_probe_oauth_quota", slow_probe)
        cfg = {
            "providers": {
                "antigravity": {
                    "type": "oauth",
                    "connections": [{"id": "cold-acc-1", "enabled": True, "access_token": "tok-cold"}],
                }
            }
        }
        monkeypatch.setattr("app.main.cs_get_config", lambda: cfg)

        cold_resp = await get_quota_status_endpoint(provider="antigravity")
        cold = json.loads(cold_resp.body)
        row = cold["providers"]["antigravity"][0]
        assert row["remaining_pct"] is None
        assert row["source"] == "unavailable"
        assert row["observed_at"] is None
        assert row["refreshing"] is True

        await asyncio.sleep(0.08)
        warm_resp = await get_quota_status_endpoint(provider="antigravity")
        warm = json.loads(warm_resp.body)
        assert warm["providers"]["antigravity"][0]["remaining_pct"] == 61.5
        await cache.close()

    asyncio.run(scenario())


def test_passive_fresher_not_overwritten_by_inflight_active_and_enriches_prior(monkeypatch):
    from app.main import _capture_rate_limit_headers, get_quota_status_endpoint
    from app.utils.quota_live import QuotaCache, account_key

    async def scenario():
        cache = QuotaCache()
        passive_by_acct = {}
        oauth_quota = {}
        monkeypatch.setattr("app.main._QUOTA_LIVE_CACHE", cache)
        monkeypatch.setattr("app.main._OAUTH_PASSIVE_BY_ACCOUNT", passive_by_acct)
        monkeypatch.setattr("app.main._OAUTH_QUOTA", oauth_quota)

        conn = {"id": "claude-acc-1", "enabled": True, "access_token": "tok-1"}
        cfg = {"providers": {"claude": {"type": "oauth", "connections": [conn]}}}
        monkeypatch.setattr("app.main.cs_get_config", lambda: cfg)

        acct = account_key(conn, 0)
        t0 = time.time() - 20.0
        cache.rows[("claude", acct)] = {
            "five_hour": {"label": "5h", "remaining_pct": 80.0},
            "weekly": {"label": "7d", "remaining_pct": 65.0},
            "remaining_pct": 65.0,
            "plan": "Max 20x",
            "source": "oauth_api",
            "observed_at": t0,
            "stale": True,
            "error": "http_503",
        }

        probe_started = asyncio.Event()
        release_probe = asyncio.Event()

        async def older_inflight_probe(*args, **kwargs):
            probe_started.set()
            await release_probe.wait()
            return {
                "five_hour": {"label": "5h", "remaining_pct": 79.0},
                "weekly": {"label": "7d", "remaining_pct": 65.0},
                "remaining_pct": 65.0,
                "source": "oauth_api",
                "observed_at": time.time() - 5.0,
            }

        monkeypatch.setattr("app.main._ensure_and_probe_oauth_quota", older_inflight_probe)

        # Trigger background active probe on stale row
        await get_quota_status_endpoint(provider="claude", force=True)
        await probe_started.wait()

        # While active probe is in flight, fresh passive 5h header arrives (42% remaining = 58% used)
        _capture_rate_limit_headers(
            "claude",
            0,
            {"anthropic-ratelimit-unified-5h-utilization": "0.58"},
        )
        mid = json.loads((await get_quota_status_endpoint(provider="claude")).body)
        mid_row = mid["providers"]["claude"][0]
        assert mid_row["five_hour"]["remaining_pct"] == 42.0
        assert mid_row["weekly"]["remaining_pct"] == 65.0
        assert mid_row["plan"] == "Max 20x"
        assert mid_row["remaining_pct"] == 42.0
        assert mid_row["source"] == "oauth_live"
        assert "error" not in mid_row

        # Release older active probe: monotonicity must reject older observed_at overwrite
        release_probe.set()
        await asyncio.sleep(0.03)
        after = json.loads((await get_quota_status_endpoint(provider="claude")).body)
        after_row = after["providers"]["claude"][0]
        assert after_row["five_hour"]["remaining_pct"] == 42.0
        assert after_row["weekly"]["remaining_pct"] == 65.0
        assert after_row["remaining_pct"] == 42.0

        # Force refresh immediately after attempt is throttled cleanly with countdown
        throttled = json.loads((await get_quota_status_endpoint(provider="claude", force=True)).body)
        t_row = throttled["providers"]["claude"][0]
        assert t_row["throttled"] is True
        assert t_row["next_refresh_in_s"] > 0
        await cache.close()

    asyncio.run(scenario())


def test_passive_quota_survives_connection_reorder(monkeypatch):
    from app.main import _capture_rate_limit_headers, get_quota_status_endpoint
    from app.utils.quota_live import QuotaCache

    async def scenario():
        cache = QuotaCache()
        monkeypatch.setattr("app.main._QUOTA_LIVE_CACHE", cache)
        monkeypatch.setattr("app.main._OAUTH_PASSIVE_BY_ACCOUNT", {})
        monkeypatch.setattr("app.main._OAUTH_QUOTA", {})

        async def idle_probe(*args, **kwargs):
            await asyncio.sleep(0.05)
            return {"remaining_pct": None, "source": "unavailable", "observed_at": None}

        monkeypatch.setattr("app.main._ensure_and_probe_oauth_quota", idle_probe)

        conn_a = {"id": "acc-alpha", "enabled": True, "access_token": "tok-a"}
        conn_b = {"id": "acc-beta", "enabled": True, "access_token": "tok-b"}
        state = {"conns": [conn_a, conn_b]}
        monkeypatch.setattr(
            "app.main.cs_get_config",
            lambda: {"providers": {"codex": {"type": "oauth", "connections": state["conns"]}}},
        )

        # Capture passive quota on index 0 (acc-alpha: 25% remaining = 75% used)
        _capture_rate_limit_headers("codex", 0, {"x-codex-primary-used-percent": "75"})

        # Reorder connections: acc-beta moves to index 0, acc-alpha moves to index 1
        state["conns"] = [conn_b, conn_a]
        data = json.loads((await get_quota_status_endpoint(provider="codex")).body)
        rows = data["providers"]["codex"]
        assert rows[0]["remaining_pct"] is None
        assert rows[1]["remaining_pct"] == 25.0
        assert rows[1]["source"] == "oauth_live"
        await cache.close()

    asyncio.run(scenario())


def test_non_oauth_billing_probe_preserved(monkeypatch):
    from app.main import get_quota_status_endpoint
    from app.utils.quota_live import QuotaCache

    async def scenario():
        cache = QuotaCache()
        monkeypatch.setattr("app.main._QUOTA_LIVE_CACHE", cache)

        async def fake_billing(base: str, key: str):
            assert base == "https://billing.example.test"
            assert key == "sk-billing-1"
            return {"remaining_pct": 88.4, "used_usd": 11.6, "hard_limit_usd": 100.0}

        monkeypatch.setattr("app.main._probe_oneapi_billing", fake_billing)
        cfg = {
            "providers": {
                "custom-openai": {
                    "type": "openai",
                    "base_url": "https://billing.example.test/",
                    "connections": [{"id": "bill-1", "enabled": True, "api_key": "sk-billing-1"}],
                }
            }
        }
        monkeypatch.setattr("app.main.cs_get_config", lambda: cfg)

        await get_quota_status_endpoint(provider="custom-openai")
        await asyncio.sleep(0.03)
        res = json.loads((await get_quota_status_endpoint(provider="custom-openai")).body)
        row = res["providers"]["custom-openai"][0]
        assert row["source"] == "billing_api"
        assert row["remaining_pct"] == 88.4
        assert row["stale"] is False
        await cache.close()

    asyncio.run(scenario())


def test_native_ide_quota_summary_wire_passthrough_and_zero_secret_leak(monkeypatch):
    """Verify verbatim byte forwarding + real Google bucketId/remainingFraction capture + D4 canary non-leak."""
    from app.main import (
        _forward_antigravity_ccpa_control,
        _record_native_ide_inference,
        get_quota_status_endpoint,
    )
    from app.utils.quota_live import QuotaCache

    async def scenario():
        cache = QuotaCache()
        obs = {}
        stats = {}
        monkeypatch.setattr("app.main._QUOTA_LIVE_CACHE", cache)
        monkeypatch.setattr("app.main._NATIVE_IDE_QUOTA_OBSERVATIONS", obs)
        monkeypatch.setattr("app.main._NATIVE_IDE_INFERENCE_STATS", stats)
        monkeypatch.setattr("app.main.cs_get_config", lambda: {"providers": {"antigravity": {"type": "oauth", "connections": []}}})

        raw_upstream_payload = json.dumps(
            {
                "groups": [
                    {
                        "displayName": "Google",
                        "buckets": [
                            {
                                "bucketId": "CARBON_5H_BUCKET",
                                "modelId": "gemini-3.1-pro-high",
                                "window": "5h",
                                "remainingFraction": 1.0,
                                "resetTime": "2026-11-16T18:00:00Z",
                                "secretTokenEcho": "DO_NOT_LEAK_CANARY_FIELD_991",
                            }
                        ],
                    }
                ],
                "internalDebugToken": "ya29.DO_NOT_LEAK_BODY_TOKEN_882",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

        chunks = [raw_upstream_payload[:47], raw_upstream_payload[47:119], raw_upstream_payload[119:]]

        class FakeUpstreamResponse:
            status_code = 200
            headers = {"content-type": "application/json; charset=utf-8"}

            async def aiter_raw(self):
                for c in chunks:
                    yield c

            async def aclose(self):
                return None

        class FakeEgressClient:
            def build_request(self, method, url, headers=None, content=b""):
                return {"method": method, "url": url}

            async def send(self, req, stream=True):
                return FakeUpstreamResponse()

        monkeypatch.setattr("app.main._get_antigravity_egress_client", lambda: FakeEgressClient())

        secret_header = "Bearer ya29.SUPER_SECRET_NATIVE_IDE_TOKEN_777"
        inf_req = _make_request(
            "POST",
            "/v1internal:streamGenerateContent",
            {"authorization": secret_header},
            b'{"model":"models/gemini-3.1-pro-high"}',
        )
        _record_native_ide_inference(inf_req, "models/gemini-3.1-pro-high")

        ctrl_req = _make_request(
            "POST",
            "/v1internal:retrieveUserQuotaSummary",
            {"authorization": secret_header, "content-type": "application/json"},
            b'{"project":"demo-proj"}',
        )
        streaming_resp = await _forward_antigravity_ccpa_control(ctrl_req, "retrieveUserQuotaSummary")
        forwarded = bytearray()
        async for chunk in streaming_resp.body_iterator:
            forwarded.extend(chunk)

        # Wire bytes must be 100% verbatim identical to upstream bytes
        assert bytes(forwarded) == raw_upstream_payload

        # Quota status endpoint exposes sanitized bucket + correlated inference count with zero secret leak (D4 canary)
        status_raw = (await get_quota_status_endpoint(provider="antigravity")).body.decode("utf-8")
        assert "SUPER_SECRET_NATIVE_IDE_TOKEN_777" not in status_raw
        assert "DO_NOT_LEAK_CANARY_FIELD_991" not in status_raw
        assert "DO_NOT_LEAK_BODY_TOKEN_882" not in status_raw

        status_json = json.loads(status_raw)
        native_rows = status_json["native_ide_passthrough"]
        assert len(native_rows) == 1
        entry = native_rows[0]
        assert len(entry["cred_id"]) == 12
        assert entry["requests_seen"] == 1
        assert entry["last_model"] == "models/gemini-3.1-pro-high"
        assert entry["buckets"] == [
            {
                "bucketId": "CARBON_5H_BUCKET",
                "modelId": "gemini-3.1-pro-high",
                "window": "5h",
                "resetTime": "2026-11-16T18:00:00Z",
                "remainingFraction": 1.0,
            }
        ]
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.anyio
async def test_parallel_reads_share_account_refresh():
    from app.utils.quota_live import QuotaCache

    cache = QuotaCache()
    calls = {"n": 0}

    async def probe():
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return {"remaining_pct": 77.0, "source": "oauth_api", "observed_at": time.time()}

    conn = {"id": "account-a"}
    first = cache.read("antigravity", conn, 0, probe, force=True)
    second = cache.read("antigravity", conn, 0, probe, force=True)
    await asyncio.sleep(0.03)
    assert first["refreshing"] is True
    assert second["refreshing"] is True
    assert calls["n"] == 1
    await cache.close()


def test_google_auxiliary_and_disabled_buckets_do_not_collapse_chat_quota(monkeypatch):
    """Auxiliary tools (grounding, autocomplete) and disabled buckets must not poison Gemini quota."""
    from app.main import _probe_oauth_quota

    class FakeGoogleResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "groups": [
                    {
                        "displayName": "Google",
                        "buckets": [
                            {
                                "displayName": "Gemini 2.5 Pro",
                                "modelId": "gemini-2.5-pro",
                                "window": "5h",
                                "remainingFraction": 0.95,
                                "resetTime": "2026-11-20T22:00:00Z",
                            },
                            {
                                "displayName": "Search Grounding",
                                "bucketId": "grounding_search",
                                "window": "5h",
                                "remainingFraction": 0.0,
                                "disabled": True,
                            },
                            {
                                "displayName": "Tab Autocomplete",
                                "bucketId": "tab_completion",
                                "window": "5h",
                                "remainingFraction": 0.05,
                            },
                        ],
                    }
                ]
            }

    monkeypatch.setattr("app.utils.google_cloudcode_egress.google_cloudcode_sync_post", lambda *a, **kw: FakeGoogleResponse())
    conn = {"api_key": "ya29.test_token", "provider_data": {"project_id": "test-proj"}}
    res = _probe_oauth_quota("antigravity", 0, conn, force=True)

    # 5h quota must reflect the chat model (95%), NOT the 0% grounding or 5% autocomplete!
    assert res["five_hour"]["remaining_pct"] == 95.0
    assert res["five_hour"]["label"] == "5h"
    assert res["groups"]["google"]["five_hour"]["remaining_pct"] == 95.0


def test_claude_cedar_ember_array_limits_fallback(monkeypatch):
    """Claude cedar_ember=1 array limits must be parsed into active windows."""
    from app.main import _probe_oauth_quota
    import httpx

    class FakeClaudeResponse:
        status_code = 200

        def json(self):
            return {
                "limits": [
                    {"kind": "session", "utilization": 0.15, "resets_at": "2026-11-20T23:00:00Z"},
                    {"kind": "weekly_all", "remaining_percentage": 88.0, "resets_at": "2026-11-27T00:00:00Z"},
                    {"kind": "fable", "remaining_percentage": 90.0, "resets_at": "2026-11-27T00:00:00Z"},
                ]
            }

    monkeypatch.setattr("app.main._OAUTH_QUOTA", {})
    monkeypatch.setattr("app.main._OAUTH_PASSIVE_BY_ACCOUNT", {})
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeClaudeResponse())
    conn = {"api_key": "sk-ant-test"}
    res = _probe_oauth_quota("claude", 0, conn, force=True)

    assert res["five_hour"]["remaining_pct"] == 85.0
    assert res["weekly"]["remaining_pct"] == 88.0


def test_google_auxiliary_buckets_at_index_zero_and_genuine_zero_exhaustion(monkeypatch):
    """Adversarial quota test: auxiliary buckets at index 0 and genuine 0% chat model exhaustion."""
    from app.main import _probe_oauth_quota

    # Case 1: Auxiliary / disabled buckets at index 0, 1 with "gemini" and "models/" in names
    class FakeAuxFirstResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "groups": [
                    {
                        "displayName": "Google",
                        "buckets": [
                            {
                                "displayName": "Gemini Search Grounding",
                                "bucketId": "grounding_search",
                                "modelId": "models/gemini-grounding",
                                "window": "5h",
                                "remainingFraction": 0.0,
                                "disabled": True,
                            },
                            {
                                "displayName": "Gemini Tab Autocomplete",
                                "bucketId": "tab_completion",
                                "modelId": "models/gemini-2.5-flash-tab-completion",
                                "window": "5h",
                                "remainingFraction": 0.05,
                            },
                            {
                                "displayName": "Gemini 2.5 Pro",
                                "modelId": "models/gemini-2.5-pro",
                                "window": "5h",
                                "remainingFraction": 0.95,
                                "resetTime": "2026-11-20T22:00:00Z",
                            },
                        ],
                    }
                ]
            }

    monkeypatch.setattr("app.utils.google_cloudcode_egress.google_cloudcode_sync_post", lambda *a, **kw: FakeAuxFirstResponse())
    conn = {"api_key": "ya29.test_token", "provider_data": {"project_id": "test-proj"}}
    res = _probe_oauth_quota("antigravity", 0, conn, force=True)
    assert res["five_hour"]["remaining_pct"] == 95.0
    assert res["groups"]["google"]["five_hour"]["remaining_pct"] == 95.0

    # Case 2: Genuine 0.0% primary chat model exhaustion followed by 100% secondary model
    class FakeGenuineZeroResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "groups": [
                    {
                        "displayName": "Google",
                        "buckets": [
                            {
                                "displayName": "Gemini 2.5 Pro",
                                "modelId": "gemini-2.5-pro",
                                "window": "5h",
                                "remainingFraction": 0.0,
                                "resetTime": "2026-11-20T22:00:00Z",
                            },
                            {
                                "displayName": "Gemini 2.5 Flash",
                                "modelId": "gemini-2.5-flash",
                                "window": "5h",
                                "remainingFraction": 1.0,
                            },
                        ],
                    }
                ]
            }

    monkeypatch.setattr("app.utils.google_cloudcode_egress.google_cloudcode_sync_post", lambda *a, **kw: FakeGenuineZeroResponse())
    res_zero = _probe_oauth_quota("antigravity", 0, conn, force=True)
    assert res_zero["five_hour"]["remaining_pct"] == 0.0
    assert res_zero["groups"]["google"]["five_hour"]["remaining_pct"] == 0.0

    # Case 3: Token boundary - "cloud-project-metadata" does not match "pro" as chat model
    class FakeProjectTokenResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "groups": [
                    {
                        "displayName": "Google",
                        "buckets": [
                            {
                                "displayName": "Project Metadata",
                                "modelId": "cloud-project-metadata",
                                "window": "5h",
                                "remainingFraction": 0.10,
                            },
                            {
                                "displayName": "Gemini 2.5 Flash",
                                "modelId": "gemini-2.5-flash",
                                "window": "5h",
                                "remainingFraction": 0.85,
                            },
                        ],
                    }
                ]
            }

    monkeypatch.setattr("app.utils.google_cloudcode_egress.google_cloudcode_sync_post", lambda *a, **kw: FakeProjectTokenResponse())
    res_proj = _probe_oauth_quota("antigravity", 0, conn, force=True)
    assert res_proj["five_hour"]["remaining_pct"] == 85.0


def test_claude_cedar_ember_exhausted_zero_and_integer_utilization(monkeypatch):
    """Claude cedar_ember limits with 100% utilization (integer 1 or float 1.0) must report 0.0% remaining."""
    from app.main import _probe_oauth_quota
    import httpx

    class FakeClaudeExhaustedResponse:
        status_code = 200

        def json(self):
            return {
                "limits": [
                    {"kind": "session", "utilization": 1, "resets_at": "2026-11-20T23:00:00Z"},
                    {"kind": "weekly_all", "remaining_percentage": 0.0, "resets_at": "2026-11-27T00:00:00Z"},
                ]
            }

    monkeypatch.setattr("app.main._OAUTH_QUOTA", {})
    monkeypatch.setattr("app.main._OAUTH_PASSIVE_BY_ACCOUNT", {})
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeClaudeExhaustedResponse())
    conn = {"api_key": "sk-ant-test"}
    res = _probe_oauth_quota("claude", 0, conn, force=True)

    assert res["five_hour"]["remaining_pct"] == 0.0
    assert res["weekly"]["remaining_pct"] == 0.0
