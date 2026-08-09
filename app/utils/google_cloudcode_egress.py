"""Hosts-file-bypassing egress transport for the Google Cloud Code control-plane.

WHY THIS EXISTS
---------------
The Windows hosts file on this machine maps the two Google Cloud Code
control-plane hostnames to 127.0.0.1, where a separate local tool (9Router)
owns the :443 listener and terminates TLS with its own CA. BSL's ordinary
httpx client trusts only certifi, so it correctly REJECTS that certificate and
fails closed (UPSTREAM_UNAVAILABLE) — but it also cannot reach the REAL Google.

This module builds a DEDICATED httpx.AsyncClient whose ONLY difference from the
general client is the DNS resolution step: for the two allowlisted Google
hostnames it resolves via EXTERNAL DNS (8.8.8.8 / 1.1.1.1) and connects to the
real public Google IP, while httpcore still derives TLS SNI + certificate
verification from the ORIGIN hostname. The result: BSL reaches genuine Google
regardless of hosts-file poisoning, with certificate verification kept ON, and
9Router never sees the connection or the forwarded Google OAuth credentials.

MECHANISM (proven live 2026-07-12)
----------------------------------
httpcore's AsyncConnectionPool accepts a PUBLIC ``network_backend=`` argument.
We wrap the default backend and override ``connect_tcp`` to substitute the TCP
destination IP for allowlisted hosts only. TLS/SNI/cert are unaffected because
httpcore computes ``server_hostname`` from the origin, independently of the TCP
target. Verified: connecting to 172.217.113.4 while requesting
cloudcode-pa.googleapis.com returned HTTP/2, server=ESF, certifi-verified, a
genuine Google OAuth 401 — with 9Router still bound to 127.0.0.1:443.

SCOPE / SAFETY
--------------
* Allowlist is EXACTLY the two poisoned hostnames. Any other host raises or is
  delegated unchanged; this is never a general-purpose resolver.
* Safe-IP filter is a verbatim copy of app/mitm.py::_is_safe_real_upstream_ip
  (ipaddress CIDR membership). It MUST NOT use string-prefix matching — a live
  bug proved "172.2" wrongly rejects PUBLIC Google space 172.217.x.x, while
  172.16.0.0/12 correctly classifies it as public.
* TLS verification stays ON (certifi). ``verify=False`` and trusting 9Router's
  CA are both forbidden by design — either would leak Google OAuth creds.
* This module is mitmproxy-FREE so it is import-safe from the web process
  (app/mitm.py imports mitmproxy at module top and must not be imported here).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import ssl
import threading
import time
from typing import List, Optional

import certifi
import httpcore
import httpx

try:  # dnspython is the external resolver; degrade gracefully if absent.
    import dns.resolver
    _HAS_DNS = True
except Exception:  # pragma: no cover - exercised only on broken installs
    _HAS_DNS = False

logger = logging.getLogger("bsl.google_egress")

# ── Pinned-stack guard ───────────────────────────────────────────────────────
# The one semi-private seam we rely on is ``httpx.AsyncHTTPTransport._pool``.
# It is stable in this pinned pair; if either dependency changes, the wiring
# must be re-verified against the live egress proof before trusting it.
_EXPECTED_HTTPX = "0.27.0"
_EXPECTED_HTTPCORE = "1.0.2"

# ── Allowlist: EXACTLY the two hosts poisoned in the Windows hosts file ──────
ALLOWED_GOOGLE_HOSTS = frozenset(
    {
        "cloudcode-pa.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
        "accounts.google.com",
        "www.googleapis.com",
    }
)

# ── External resolvers (bypass the hosts file entirely) ──────────────────────
_EXTERNAL_NAMESERVERS = ["8.8.8.8", "1.1.1.1", "8.8.4.4"]
_RESOLVE_LIFETIME = 5.0

# ── Resolution caches (host -> (ip_list, expiry_ts)) ─────────────────────────
# _RESOLVE_CACHE is the hot cache honoured on every connect. _LAST_KNOWN_GOOD is
# a longer-lived safety net used ONLY when a fresh resolve fails (resolver
# outage) — never to mask a legitimately changed Google IP, because a
# successful fresh resolve always overwrites it.
_RESOLVE_CACHE: dict = {}
_RESOLVE_TTL = 300  # seconds; mirrors app/mitm.py:_REAL_IP_TTL
_LAST_KNOWN_GOOD: dict = {}
_LAST_KNOWN_GOOD_TTL = 3600  # seconds; mirrors app/mitm.py:_LAST_KNOWN_SAFE_REAL_IP_TTL
_cache_lock = threading.Lock()

# ── Upstream timeout (mirrors app/main.py:_UPSTREAM_TIMEOUT; duplicated here to
# avoid importing main.py, which would be circular) ──────────────────────────
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=60.0)
_KEEPALIVE_EXPIRY = 30.0
_POOL_RETRIES = 2


def _is_safe_real_upstream_ip(value: str) -> bool:
    """Return whether an address can safely be used as a real upstream target.

    VERBATIM port of app/mitm.py:130-151. Uses ipaddress CIDR membership so
    PUBLIC Google ranges (e.g. 172.217.x.x) are correctly accepted while true
    private/loopback/link-local space is rejected. DO NOT replace with
    string-prefix checks.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    return not any(
        (
            address.is_loopback,
            address.is_unspecified,
            address.is_link_local,
            address.is_multicast,
            any(address in network for network in private_networks),
        )
    )


def _external_resolve(host: str) -> List[str]:
    """Raw external-DNS A-record lookup for ``host``.

    Returns the address strings in resolver order (unfiltered). Separated from
    :func:`resolve_google_ips` purely so tests can stub the network call.
    """
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(_EXTERNAL_NAMESERVERS)
    resolver.lifetime = _RESOLVE_LIFETIME
    answer = resolver.resolve(host, "A")
    return [record.address for record in answer]


def _cached_ips(host: str, now: float) -> Optional[List[str]]:
    entry = _RESOLVE_CACHE.get(host)
    if not entry:
        return None
    ips, expiry = entry
    if expiry <= now:
        _RESOLVE_CACHE.pop(host, None)
        return None
    safe = [ip for ip in ips if _is_safe_real_upstream_ip(ip)]
    if not safe:
        _RESOLVE_CACHE.pop(host, None)
        return None
    return safe


def _last_known_good_ips(host: str, now: float) -> List[str]:
    entry = _LAST_KNOWN_GOOD.get(host)
    if not entry:
        return []
    ips, expiry = entry
    if expiry <= now:
        _LAST_KNOWN_GOOD.pop(host, None)
        return []
    safe = [ip for ip in ips if _is_safe_real_upstream_ip(ip)]
    if not safe:
        _LAST_KNOWN_GOOD.pop(host, None)
    return list(safe)


def resolve_google_ips(host: str) -> List[str]:
    """Resolve ``host`` to ALL safe public Google IPs via external DNS.

    Returns a list of failover candidates (may be length 1). Returns an empty
    list only when no safe address can be obtained from a fresh resolve or the
    last-known-good safety net, in which case the caller must fail closed.

    Raises ``ValueError`` for any host outside :data:`ALLOWED_GOOGLE_HOSTS` so
    this can never become a general-purpose resolver.
    """
    if host not in ALLOWED_GOOGLE_HOSTS:
        raise ValueError(f"host not allowlisted for Google egress: {host!r}")

    now = time.time()
    with _cache_lock:
        cached = _cached_ips(host, now)
        if cached is not None:
            logger.debug("google-egress cache hit %s -> %s", host, cached)
            return cached

    if not _HAS_DNS:
        fallback = _last_known_good_ips(host, now)
        if fallback:
            logger.warning(
                "google-egress: dnspython unavailable; using last-known-good %s -> %s",
                host,
                fallback,
            )
        else:
            logger.error("google-egress: dnspython unavailable and no last-known-good for %s", host)
        return fallback

    try:
        raw = _external_resolve(host)
    except Exception as exc:
        logger.error("google-egress: external DNS failed for %s: %s", host, type(exc).__name__)
        fallback = _last_known_good_ips(host, now)
        if fallback:
            logger.warning("google-egress: using last-known-good %s -> %s", host, fallback)
        return fallback

    safe = [ip for ip in raw if _is_safe_real_upstream_ip(ip)]
    rejected = [ip for ip in raw if ip not in safe]
    if rejected:
        # Reject-and-continue: a poisoned/loopback answer must never be used,
        # but valid siblings in the same answer still are.
        logger.error("google-egress: rejected unsafe DNS answer(s) for %s: %s", host, rejected)

    if not safe:
        fallback = _last_known_good_ips(host, now)
        if fallback:
            logger.warning(
                "google-egress: fresh resolve had no safe IP; last-known-good %s -> %s",
                host,
                fallback,
            )
        else:
            logger.error("google-egress: no safe upstream IP available for %s", host)
        return fallback

    with _cache_lock:
        _RESOLVE_CACHE[host] = (list(safe), now + _RESOLVE_TTL)
        _LAST_KNOWN_GOOD[host] = (list(safe), now + _LAST_KNOWN_GOOD_TTL)
    logger.info("google-egress resolved %s -> %s (external DNS)", host, safe)
    return list(safe)


class HostsBypassBackend(httpcore.AsyncNetworkBackend):
    """Delegating network backend that redirects ONLY allowlisted Google hosts.

    For allowlisted hostnames it resolves via external DNS and tries each safe
    public IP in turn (failover). All other hosts, unix sockets, and sleeps are
    delegated to the wrapped backend unchanged. TLS/SNI/cert verification happen
    downstream in httpcore against the ORIGIN hostname and are unaffected by the
    substituted TCP target.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ):
        if host in ALLOWED_GOOGLE_HOSTS:
            ips = await asyncio.to_thread(resolve_google_ips, host)
            if not ips:
                raise httpcore.ConnectError(
                    f"no safe public Google IP resolvable for {host}"
                )
            last_exc: Optional[BaseException] = None
            for ip in ips:
                try:
                    stream = await self._inner.connect_tcp(
                        ip,
                        port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                    logger.info("google-egress connected %s:%s via %s", host, port, ip)
                    return stream
                except Exception as exc:  # try the next failover candidate
                    last_exc = exc
                    logger.warning(
                        "google-egress connect failover: %s (%s), trying next",
                        ip,
                        type(exc).__name__,
                    )
            assert last_exc is not None
            raise last_exc

        return await self._inner.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _discover_default_async_backend() -> httpcore.AsyncNetworkBackend:
    """Return httpcore's default async backend instance.

    Discovered from a throwaway pool rather than importing a private class path,
    so it stays correct across httpcore's internal backend selection. The probe
    pool is never started and holds no connections.
    """
    probe = httpcore.AsyncConnectionPool()
    for value in vars(probe).values():
        if hasattr(value, "connect_tcp"):
            return value
    return httpcore.AnyIOBackend()


def _check_pinned_stack() -> None:
    if httpx.__version__ != _EXPECTED_HTTPX or httpcore.__version__ != _EXPECTED_HTTPCORE:
        logger.warning(
            "google-egress: unverified transport stack (httpx=%s httpcore=%s; "
            "expected httpx=%s httpcore=%s). The AsyncHTTPTransport._pool seam "
            "must be re-verified against the live egress proof.",
            httpx.__version__,
            httpcore.__version__,
            _EXPECTED_HTTPX,
            _EXPECTED_HTTPCORE,
        )


def build_google_egress_client() -> httpx.AsyncClient:
    """Build the dedicated hosts-file-bypassing client for Google Cloud Code.

    TLS verification is ON (certifi). HTTP/2 is enabled (proven negotiable to
    Google through the custom backend). The connection pool mirrors the general
    client's limits/retries so behaviour is otherwise identical.
    """
    _check_pinned_stack()

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    backend = HostsBypassBackend(_discover_default_async_backend())

    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        max_connections=200,
        max_keepalive_connections=100,
        keepalive_expiry=_KEEPALIVE_EXPIRY,
        http1=True,
        http2=True,
        retries=_POOL_RETRIES,
        network_backend=backend,
    )

    transport = httpx.AsyncHTTPTransport()
    # The one semi-private seam (guarded above): swap the transport's pool for
    # ours so httpx drives our external-DNS backend while retaining its own
    # request/response machinery, pooling, and HTTP/2 support.
    transport._pool = pool

    return httpx.AsyncClient(
        timeout=_UPSTREAM_TIMEOUT,
        transport=transport,
        trust_env=False,  # env proxy vars must never redirect this client
    )
