"""Unit tests for the Google Cloud Code hosts-file-bypassing egress transport.

These tests never touch the real network: DNS is stubbed at
``_external_resolve`` and the httpcore backend's inner is a fake. They lock in
the safety-critical invariants:
  * the ipaddress-based safe-IP filter (drift guard against the string-prefix
    bug that wrongly rejected PUBLIC Google space 172.217.x.x),
  * allowlist-only resolution (never a general-purpose resolver),
  * reject-unsafe-but-keep-safe DNS filtering,
  * caching returns the FULL failover LIST (not a single IP),
  * TTL expiry re-resolves,
  * resolver-outage last-known-good fallback,
  * multi-IP connect failover,
  * pass-through for non-allowlisted hosts.
"""
import asyncio
import os
import sys

import httpcore
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.utils import google_cloudcode_egress as egress

CLOUDCODE = "cloudcode-pa.googleapis.com"
DAILY = "daily-cloudcode-pa.googleapis.com"


@pytest.fixture(autouse=True)
def _reset_egress_state():
    """Isolate module-level caches and force dnspython-present between tests."""
    egress._RESOLVE_CACHE.clear()
    egress._LAST_KNOWN_GOOD.clear()
    original_has_dns = egress._HAS_DNS
    egress._HAS_DNS = True
    yield
    egress._RESOLVE_CACHE.clear()
    egress._LAST_KNOWN_GOOD.clear()
    egress._HAS_DNS = original_has_dns


# ── 1. Safe-IP filter DRIFT GUARD ────────────────────────────────────────────
def test_is_safe_ip_accepts_public_google_and_rejects_private():
    # PUBLIC Google ranges MUST be accepted. 172.217.x.x is the live range that
    # a naive "172.2" string-prefix check wrongly rejected.
    for ip in ("172.217.113.4", "172.217.119.4", "216.239.38.223", "216.239.32.223"):
        assert egress._is_safe_real_upstream_ip(ip) is True, ip
    # Loopback / private / link-local / multicast / unspecified MUST be rejected.
    for ip in (
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
    ):
        assert egress._is_safe_real_upstream_ip(ip) is False, ip
    # Garbage input fails closed.
    assert egress._is_safe_real_upstream_ip("not-an-ip") is False


# ── 2. Allowlist-only resolution ─────────────────────────────────────────────
def test_resolve_rejects_non_allowlisted_host():
    with pytest.raises(ValueError):
        egress.resolve_google_ips("evil.example.com")
    with pytest.raises(ValueError):
        egress.resolve_google_ips("googleapis.com")


# ── 3. Reject unsafe answers, keep safe ones ─────────────────────────────────
def test_resolve_drops_unsafe_records_and_keeps_safe(monkeypatch):
    monkeypatch.setattr(
        egress,
        "_external_resolve",
        lambda host: ["127.0.0.1", "172.217.113.4", "10.0.0.5", "172.217.118.4"],
    )
    result = egress.resolve_google_ips(CLOUDCODE)
    assert result == ["172.217.113.4", "172.217.118.4"]


def test_resolve_returns_empty_when_all_unsafe(monkeypatch):
    monkeypatch.setattr(egress, "_external_resolve", lambda host: ["127.0.0.1", "10.0.0.5"])
    assert egress.resolve_google_ips(CLOUDCODE) == []


# ── amendment #2: cache stores the FULL failover LIST, not a single IP ───────
def test_resolve_returns_and_caches_full_list(monkeypatch):
    monkeypatch.setattr(
        egress, "_external_resolve", lambda host: ["172.217.113.4", "172.217.118.4", "172.217.119.4"]
    )
    result = egress.resolve_google_ips(CLOUDCODE)
    assert result == ["172.217.113.4", "172.217.118.4", "172.217.119.4"]
    # The hot cache must hold the whole list so failover candidates survive a hit.
    cached_ips, _expiry = egress._RESOLVE_CACHE[CLOUDCODE]
    assert cached_ips == ["172.217.113.4", "172.217.118.4", "172.217.119.4"]


# ── 4. Caching: hit within TTL, re-resolve after expiry ──────────────────────
def test_cache_hit_within_ttl_then_reresolve_after_expiry(monkeypatch):
    calls = {"n": 0}

    def fake_resolve(host):
        calls["n"] += 1
        return ["172.217.113.4", "172.217.118.4"]

    monkeypatch.setattr(egress, "_external_resolve", fake_resolve)

    first = egress.resolve_google_ips(CLOUDCODE)
    second = egress.resolve_google_ips(CLOUDCODE)  # within TTL -> cache hit
    assert first == second == ["172.217.113.4", "172.217.118.4"]
    assert calls["n"] == 1, "second call within TTL must not re-resolve"

    # Force expiry deterministically (no sleep, no global clock patch).
    import time as _t

    ips, _old = egress._RESOLVE_CACHE[CLOUDCODE]
    egress._RESOLVE_CACHE[CLOUDCODE] = (ips, _t.time() - 1)

    third = egress.resolve_google_ips(CLOUDCODE)
    assert third == ["172.217.113.4", "172.217.118.4"]
    assert calls["n"] == 2, "expired cache must trigger a fresh resolve"


# ── 5. Resolver-outage last-known-good fallback ──────────────────────────────
def test_resolver_outage_uses_last_known_good(monkeypatch):
    # Seed a successful resolve to populate last-known-good.
    monkeypatch.setattr(egress, "_external_resolve", lambda host: ["172.217.113.4"])
    egress.resolve_google_ips(CLOUDCODE)

    # Now the hot cache is dropped and the resolver starts failing.
    egress._RESOLVE_CACHE.clear()

    def boom(host):
        raise RuntimeError("dns down")

    monkeypatch.setattr(egress, "_external_resolve", boom)
    result = egress.resolve_google_ips(CLOUDCODE)
    assert result == ["172.217.113.4"], "must fall back to last-known-good on outage"


def test_resolver_outage_without_last_known_good_returns_empty(monkeypatch):
    def boom(host):
        raise RuntimeError("dns down")

    monkeypatch.setattr(egress, "_external_resolve", boom)
    assert egress.resolve_google_ips(DAILY) == []


# ── 6 & failover: HostsBypassBackend tries each safe IP in order ─────────────
class _FakeInnerBackend:
    """Minimal stand-in for httpcore's default async backend."""

    def __init__(self, fail_targets=()):
        self.fail_targets = set(fail_targets)
        self.attempts = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.attempts.append(host)
        if host in self.fail_targets:
            raise httpcore.ConnectError(f"refused {host}")
        return f"stream->{host}:{port}"

    async def connect_unix_socket(self, *a, **k):  # pragma: no cover - not exercised
        return "unix"

    async def sleep(self, seconds):  # pragma: no cover - not exercised
        return None


def test_backend_failover_tries_second_ip(monkeypatch):
    monkeypatch.setattr(egress, "resolve_google_ips", lambda host: ["203.0.113.1", "203.0.113.2"])
    inner = _FakeInnerBackend(fail_targets={"203.0.113.1"})
    backend = egress.HostsBypassBackend(inner)

    stream = asyncio.run(backend.connect_tcp(CLOUDCODE, 443))
    assert stream == "stream->203.0.113.2:443"
    assert inner.attempts == ["203.0.113.1", "203.0.113.2"], "must try IPs in order"


def test_backend_raises_connect_error_when_no_ips(monkeypatch):
    monkeypatch.setattr(egress, "resolve_google_ips", lambda host: [])
    backend = egress.HostsBypassBackend(_FakeInnerBackend())
    with pytest.raises(httpcore.ConnectError):
        asyncio.run(backend.connect_tcp(CLOUDCODE, 443))


def test_backend_raises_last_error_when_all_ips_fail(monkeypatch):
    monkeypatch.setattr(egress, "resolve_google_ips", lambda host: ["203.0.113.1", "203.0.113.2"])
    inner = _FakeInnerBackend(fail_targets={"203.0.113.1", "203.0.113.2"})
    backend = egress.HostsBypassBackend(inner)
    with pytest.raises(httpcore.ConnectError):
        asyncio.run(backend.connect_tcp(CLOUDCODE, 443))
    assert inner.attempts == ["203.0.113.1", "203.0.113.2"]


# ── 7. Non-allowlisted hosts delegate UNCHANGED (no IP rewrite) ──────────────
def test_backend_passes_through_non_allowlisted_host():
    inner = _FakeInnerBackend()
    backend = egress.HostsBypassBackend(inner)
    stream = asyncio.run(backend.connect_tcp("example.com", 443))
    assert stream == "stream->example.com:443"
    assert inner.attempts == ["example.com"], "non-allowlisted host must not be rewritten to an IP"


# ── Builder smoke: constructs a client with our custom pool wired in ─────────
def test_build_client_wires_custom_backend():
    client = egress.build_google_egress_client()
    try:
        pool = client._transport._pool
        assert isinstance(pool, httpcore.AsyncConnectionPool)
        assert isinstance(pool._network_backend, egress.HostsBypassBackend)
        assert client._trust_env is False
    finally:
        asyncio.run(client.aclose())
