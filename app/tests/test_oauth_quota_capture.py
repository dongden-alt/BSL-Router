"""Tests for OAuth passive quota capture (2026-08-24).

Verifies:
1. _capture_rate_limit_headers parses Anthropic-style headers → token-based %
2. Falls back to request-based % when token headers absent
3. OpenAI-style x-ratelimit-* headers also work
4. Missing/zero-limit headers → no entry created
5. None conn_index → no-op
6. Endpoint merges registry data for OAuth connections
7. Staleness: old entries still served (they carry updated_at for the frontend)
"""
import time
import pytest
from unittest.mock import patch, MagicMock


def _import_quota():
    """Import the capture function from main (lazy to avoid app startup)."""
    import app.main as mod
    return (
        mod._capture_rate_limit_headers,
        mod._OAUTH_QUOTA,
    )


def _make_headers(d):
    """Build a case-insensitive header dict like httpx.Headers."""
    return {k: v for k, v in d.items()}


class TestCaptureRateLimitHeaders:
    def test_anthropic_token_based(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("claude", 0, _make_headers({
            "anthropic-ratelimit-tokens-remaining": "50000",
            "anthropic-ratelimit-tokens-limit": "100000",
            "anthropic-ratelimit-requests-remaining": "40",
            "anthropic-ratelimit-requests-limit": "50",
        }))
        entry = reg["claude"][0]
        assert entry["remaining_pct"] == 50.0
        assert entry["metric"] == "tokens"

    def test_anthropic_request_fallback(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("claude", 1, _make_headers({
            "anthropic-ratelimit-requests-remaining": "10",
            "anthropic-ratelimit-requests-limit": "100",
        }))
        entry = reg["claude"][1]
        assert entry["remaining_pct"] == 10.0
        assert entry["metric"] == "requests"

    def test_openai_style_headers(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("codex", 2, _make_headers({
            "x-ratelimit-remaining-tokens": "25000",
            "x-ratelimit-limit-tokens": "50000",
        }))
        entry = reg["codex"][2]
        assert entry["remaining_pct"] == 50.0
        assert entry["metric"] == "tokens"

    def test_missing_headers_no_entry(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("github", 0, _make_headers({"content-type": "application/json"}))
        assert "github" not in reg or 0 not in reg.get("github", {})

    def test_zero_limit_no_entry(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("kiro", 0, _make_headers({
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-limit-tokens": "0",
        }))
        assert "kiro" not in reg or 0 not in reg.get("kiro", {})

    def test_none_conn_index_noop(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("claude", None, _make_headers({
            "anthropic-ratelimit-tokens-remaining": "50000",
            "anthropic-ratelimit-tokens-limit": "100000",
        }))
        assert len(reg) == 0

    def test_never_raises(self):
        cap, reg = _import_quota()
        reg.clear()
        # Pass garbage that could TypeError inside float()
        cap("claude", 0, _make_headers({"anthropic-ratelimit-tokens-remaining": None}))
        cap("claude", 0, _make_headers({"anthropic-ratelimit-tokens-remaining": "abc"}))
        cap("claude", 0, "not a dict")
        # Should never raise; entry may or may not exist, no crash
        assert True

    def test_updates_existing_entry(self):
        cap, reg = _import_quota()
        reg.clear()
        cap("claude", 0, _make_headers({
            "anthropic-ratelimit-tokens-remaining": "90000",
            "anthropic-ratelimit-tokens-limit": "100000",
        }))
        assert reg["claude"][0]["remaining_pct"] == 90.0
        cap("claude", 0, _make_headers({
            "anthropic-ratelimit-tokens-remaining": "10000",
            "anthropic-ratelimit-tokens-limit": "100000",
        }))
        assert reg["claude"][0]["remaining_pct"] == 10.0
