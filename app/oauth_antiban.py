"""OAuth anti-ban: session-sticky account affinity + account-keyed egress isolation.

Why this exists (evidence, not theory):
  Inbound capture logs (.brain/logs/antigravity_inbound.jsonl) show coding-CLI
  request cadence is already human-paced (p50=14s, p90=51s). A blocking
  request-level throttle therefore adds latency to organic traffic and buys
  zero anti-ban. The real proxy signature is BEHAVIOURAL + NETWORK, not speed:
    * round-robin across N accounts within one IDE session  -> one "user"
      holding N concurrent identities (verified 5-account Antigravity lockout)
    * N accounts egressing from one IP / one shared connection pool
  This module kills both without touching cadence.

Two primitives, both fail-open and composed over (never forked from)
app.utils.model_resolver and app.circuit_breaker:

  1. AffinityStore / apply_affinity
     Pin (session_key, provider) -> connection original_index. Implemented as a
     PRE-FILTER on the eligible pool before the resolver picks, so it is
     strategy-agnostic (works identically whether the provider is top-first or
     round_robin) and leaves non-OAuth providers' pools untouched.

  2. get_client_for_account
     An account-keyed httpx.AsyncClient cache so each account can carry its own
     egress proxy (per-account IP isolation) and never shares a keep-alive
     socket with a sibling account.

Config (all additive, all optional) under tools.oauth_antiban:
    enabled: true
    affinity_ttl_seconds: 1800
    affinity_max_sessions: 512
    session_key_fields: [metadata.session_id, prompt_cache_key, user]

Absent the block entirely, affinity defaults ON for OAUTH_AFFINITY_PROVIDERS
and OFF everywhere else.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except Exception:  # pragma: no cover - httpx always present in prod
    httpx = None  # type: ignore


# Providers this module governs by default. Antigravity (P1), OpenAI Codex (P2),
# Claude Code (P3). A provider is ALSO treated as OAuth when any connection
# carries an OAuth marker, so custom-named OAuth lanes still get affinity.
OAUTH_AFFINITY_PROVIDERS = frozenset({
    "antigravity",
    "codex",
    "claude_code",
    "claude-code",
    "claudecode",
})

# Per-connection markers that identify an OAuth credential. We deliberately
# never key identity off the raw token; stable non-secret identities only.
_OAUTH_CONN_MARKERS = ("refresh_token", "oauth", "oauth_email", "id")

_DEFAULT_TTL_SECONDS = 1800
_DEFAULT_MAX_SESSIONS = 512
_DEFAULT_CLIENT_CACHE = 64


def resolve_antiban_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve tools.oauth_antiban with safe defaults. Pure, never mutates."""
    tools = (config or {}).get("tools", {})
    raw = tools.get("oauth_antiban") if isinstance(tools, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "affinity_ttl_seconds": int(raw.get("affinity_ttl_seconds", _DEFAULT_TTL_SECONDS)),
        "affinity_max_sessions": int(raw.get("affinity_max_sessions", _DEFAULT_MAX_SESSIONS)),
        "session_key_fields": list(raw.get(
            "session_key_fields",
            ["metadata.session_id", "metadata.conversation_id",
             "metadata.user_id", "prompt_cache_key", "user"],
        )),
        "client_cache_size": int(raw.get("client_cache_size", _DEFAULT_CLIENT_CACHE)),
    }


def is_oauth_connection(conn: Any) -> bool:
    """True only when THIS connection carries an OAuth credential marker.

    Name-level detection (`is_oauth_provider`) is deliberately looser: it exists
    to decide whether session AFFINITY should pin a provider's pool. Egress
    CLIENT isolation is a stronger, per-credential decision, so it requires real
    evidence on the active row — an OAuth-marked lane registered under the name
    "codex" can still hold plain API-key connections, and those must stay on the
    shared proxy client path (same keep-alive pool, same proxy, same test seam).
    """
    if not isinstance(conn, dict):
        return False
    return any(conn.get(m) for m in _OAUTH_CONN_MARKERS)


def is_oauth_provider(provider_name: str, provider_config: Optional[Dict[str, Any]] = None) -> bool:
    """True when affinity should govern this provider.

    Default-on for the three known OAuth providers; also true when any
    connection carries an OAuth marker so custom-named OAuth lanes are covered.
    """
    if (provider_name or "").lower() in OAUTH_AFFINITY_PROVIDERS:
        return True
    for conn in (provider_config or {}).get("connections", []) or []:
        if is_oauth_connection(conn):
            return True
    return False


def _dig(body: Dict[str, Any], dotted: str) -> Optional[str]:
    """Resolve a dotted field path ("metadata.session_id") against body."""
    node: Any = body
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return str(node) if node not in (None, "") else None


def derive_session_key(
    body: Optional[Dict[str, Any]],
    request: Any = None,
    fields: Optional[List[str]] = None,
) -> str:
    """Evidence-graded session identity, with graceful degradation.

    Order (first hit wins):
      1. configured body identity fields (metadata.session_id / conversation_id /
         user_id / prompt_cache_key / user)
      2. fallback: client host + User-Agent (the IDE is a single local client,
         so this is one affinity bucket -- coarse but still correct)
      3. constant "anon" when even that is unavailable.

    Never raises; always returns a non-empty string.
    """
    body = body if isinstance(body, dict) else {}
    for dotted in (fields or []):
        try:
            val = _dig(body, dotted)
        except Exception:
            val = None
        if val:
            return "f:%s:%s" % (dotted, val)

    host = ""
    ua = ""
    try:
        if request is not None:
            client = getattr(request, "client", None)
            host = getattr(client, "host", "") or ""
            headers = getattr(request, "headers", None) or {}
            ua = headers.get("user-agent", "") if hasattr(headers, "get") else ""
    except Exception:
        host, ua = "", ""
    if host or ua:
        return "fb:%s|%s" % (host, ua[:64])
    return "anon"


class AffinityStore:
    """Bounded TTL+LRU map: (session_key, provider) -> connection original_index.

    In-memory only by design -- a restart is a fresh session cohort upstream, so
    rebinding on boot is correct, not a regression. Thread-safe.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS, max_sessions: int = _DEFAULT_MAX_SESSIONS):
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(1, int(max_sessions))
        self._data: "OrderedDict[Tuple[str, str], Tuple[int, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, session_key: str, provider: str) -> Optional[int]:
        """Return bound index if present and unexpired; refresh LRU recency."""
        key = (session_key, provider)
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            index, ts = entry
            if now - ts > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return index

    def bind(self, session_key: str, provider: str, index: int) -> None:
        key = (session_key, provider)
        with self._lock:
            self._data[key] = (int(index), time.monotonic())
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def release(self, session_key: str, provider: str) -> None:
        with self._lock:
            self._data.pop((session_key, provider), None)

    def __len__(self) -> int:  # pragma: no cover - trivial
        with self._lock:
            return len(self._data)


# --- Request-scoped session identity (set by main's dispatch entry) ---
# The resolver hook has no access to the request body, so main publishes the
# derived session key through this contextvar. Each FastAPI request runs in its
# own asyncio task, so the value dies with the task; reset is optional.
_REQUEST_SESSION_KEY: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "bsl_oauth_antiban_session_key", default=None
)


def set_request_session_key(key: Optional[str]):
    """Bind a session identity to the current request/task context.

    Returns the contextvars Token so callers MAY reset explicitly; per-request
    task isolation makes reset optional.
    """
    return _REQUEST_SESSION_KEY.set(key or None)


def reset_request_session_key(token) -> None:
    try:
        _REQUEST_SESSION_KEY.reset(token)
    except Exception:
        pass


def current_request_session_key() -> Optional[str]:
    try:
        return _REQUEST_SESSION_KEY.get()
    except Exception:
        return None


# Process-global store (matches circuit_breaker / RR-state module-global style).
_STORE = AffinityStore()


def get_store() -> AffinityStore:
    return _STORE


def reset_store() -> None:
    """Test/config-reload hook: drop all bindings."""
    global _STORE
    _STORE = AffinityStore()


def apply_affinity(
    provider_name: str,
    provider_config: Dict[str, Any],
    eligible: List[Dict[str, Any]],
    session_key: str,
    model_id: str = "",
    breaker: Any = None,
    config: Optional[Dict[str, Any]] = None,
    store: Optional[AffinityStore] = None,
) -> List[Dict[str, Any]]:
    """Pre-filter `eligible` (shape [{"index","conn"},...]) to the sticky account.

    Returns the pool UNCHANGED for non-OAuth providers, when disabled, or on
    any internal error (fail-open). On a sticky HIT the pool collapses to the
    bound index unless the breaker reports it unhealthy, in which case the
    binding is released and a fresh (healthy) index is bound.
    """
    try:
        cfg = resolve_antiban_config(config)
        if not cfg["enabled"]:
            return eligible
        if not is_oauth_provider(provider_name, provider_config):
            return eligible
        if not eligible:
            return eligible

        store = store or _STORE
        by_index = {e["index"]: e for e in eligible if isinstance(e, dict) and "index" in e}
        if not by_index:
            return eligible

        bound = store.lookup(session_key, provider_name)
        if bound is not None and bound in by_index:
            if _is_healthy(breaker, provider_name, model_id, bound):
                return [by_index[bound]]
            # Sticky account tripped the breaker -> rebind once to a healthy peer.
            store.release(session_key, provider_name)
            print(
                "[affinity] rebind provider=%s reason=breaker-open released_idx=%s"
                % (provider_name, bound),
                flush=True,
            )

        # MISS (or rebind): pick the first healthy eligible index, deterministically.
        for e in eligible:
            idx = e.get("index") if isinstance(e, dict) else None
            if idx is None:
                continue
            if _is_healthy(breaker, provider_name, model_id, idx):
                store.bind(session_key, provider_name, idx)
                return [e]
        return eligible  # nothing healthy -> leave pool, resolver/breaker decides
    except Exception as exc:  # fail-open, never break routing
        print("[affinity] fail-open (%s)" % type(exc).__name__, flush=True)
        return eligible


def _is_healthy(breaker: Any, provider_name: str, model_id: str, index: int) -> bool:
    """Best-effort breaker health probe across its known API shapes.

    True (healthy) when: no breaker, breaker disabled, or the index is not
    OPEN. The real CircuitBreaker.is_open(provider, model, conn_index) returns
    a (is_open, seconds_remaining) tuple; tolerate bare-bool stubs too.
    Any probe failure -> True (fail-open, matching breaker philosophy).
    """
    if breaker is None or not getattr(breaker, "enabled", False):
        return True
    try:
        if hasattr(breaker, "is_open"):
            res = breaker.is_open(provider_name, model_id, index)
            if isinstance(res, tuple):
                return not bool(res[0])
            return not bool(res)
        if hasattr(breaker, "can_attempt"):
            return bool(breaker.can_attempt(provider_name, index))
        if hasattr(breaker, "allow"):
            return bool(breaker.allow(provider_name, index))
        if hasattr(breaker, "get_state"):
            return str(breaker.get_state(provider_name, index)).upper() != "OPEN"
    except Exception:
        return True
    return True


def resolver_affinity_hook(
    provider_name: str,
    provider_config: Dict[str, Any],
    eligible: List[Dict[str, Any]],
    model_id: str = "",
    breaker: Any = None,
) -> List[Dict[str, Any]]:
    """Adapter registered into app.utils.model_resolver.set_affinity_hook.

    Reads the request-scoped session key from the contextvar (published by
    main's dispatch entry). No key -> pool untouched (scouts, background
    tasks, non-request contexts). Never raises; the resolver additionally
    wraps the call in its own fail-open try/except.
    """
    try:
        sk = current_request_session_key()
        if not sk:
            return eligible
        return apply_affinity(
            provider_name, provider_config, eligible, sk,
            model_id=model_id, breaker=breaker, config=None,
        )
    except Exception:
        return eligible


# --- Account-keyed egress client cache ---

_CLIENTS: "OrderedDict[Tuple[str, str, bool], Any]" = OrderedDict()
_CLIENTS_LOCK = threading.Lock()


def account_identity(conn: Dict[str, Any], fallback_index: Optional[int] = None) -> str:
    """Stable, non-secret per-account identity for cache keying.

    Prefers connection id, then oauth_email, then original index. NEVER the
    token/api_key.
    """
    for k in ("id", "oauth_email", "email", "name"):
        v = conn.get(k)
        if v:
            return str(v)
    return "idx:%s" % fallback_index if fallback_index is not None else "anon"


def get_client_for_account(
    account_id: str,
    proxy_url: Optional[str],
    verify: bool = True,
    build_client=None,
    cache_size: int = _DEFAULT_CLIENT_CACHE,
):
    """Return a cached AsyncClient keyed by (account, proxy, verify).

    Guarantees two accounts never share a keep-alive socket, and lets each
    account pin its own egress proxy (per-account IP isolation). LRU-bounded;
    evicted clients are closed best-effort. build_client is injected for tests
    and by main to supply _build_hardened_client without a circular import.
    """
    if httpx is None:  # pragma: no cover
        return None
    key = (account_id, proxy_url or "direct", bool(verify))
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is not None:
            _CLIENTS.move_to_end(key)
            return client

    if build_client is None:
        # Bounded connect/write/pool so a dead upstream fails fast instead of
        # parking the event loop forever; read stays unbounded for SSE streams.
        _fallback_timeout = httpx.Timeout(
            connect=15.0, read=None, write=15.0, pool=15.0
        )

        def build_client(proxy_url=proxy_url, verify=verify):
            return httpx.AsyncClient(proxy=proxy_url, verify=verify, timeout=_fallback_timeout)

    client = build_client(proxy_url=proxy_url, verify=verify)

    with _CLIENTS_LOCK:
        _CLIENTS[key] = client
        _CLIENTS.move_to_end(key)
        while len(_CLIENTS) > max(1, int(cache_size)):
            _, evicted = _CLIENTS.popitem(last=False)
            _close_client_best_effort(evicted)
    return client


def _close_client_best_effort(client: Any) -> None:
    """Close an evicted client without raising; schedule on a live loop if any."""
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(client.aclose())
        # No running loop: drop the reference; GC closes the transport.
    except Exception:
        pass


def reset_clients() -> None:
    """Test/config-reload hook: drop all cached clients (best-effort close)."""
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for c in clients:
        _close_client_best_effort(c)