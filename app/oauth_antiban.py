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
import random
import threading
import time
from collections import OrderedDict, deque
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
    "claude",
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

_CLAUDE_FAMILY = frozenset({"claude", "claude_code", "claude-code", "claudecode"})

_DEFAULTS = {
    "enabled": True,
    "affinity_ttl_seconds": _DEFAULT_TTL_SECONDS,
    "affinity_max_sessions": _DEFAULT_MAX_SESSIONS,
    "client_cache_size": _DEFAULT_CLIENT_CACHE,
    # G1 cadence
    "min_interval_seconds": 0.9,
    "cadence_jitter": 0.35,
    # G2 adaptive 429 backoff
    "backoff_base_seconds": 1.0,
    "backoff_max_seconds": 90.0,
    "backoff_max_retries": 4,
    # G3 lane pinning
    "session_ttl_seconds": 1800,
    # G6 usage budget
    "usage_window_seconds": 18000,
    "usage_budget_fraction": 0.9,
    # G5 authenticity
    "authenticity_threshold": 0.55,
    "authenticity_enforce": False,  # default WARN-only; flip to block
}


def resolve_antiban_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve tools.oauth_antiban with safe defaults. Pure, never mutates."""
    tools = (config or {}).get("tools", {})
    raw = tools.get("oauth_antiban") if isinstance(tools, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(_DEFAULTS)
    for k in _DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    out["enabled"] = bool(out["enabled"])
    out["authenticity_enforce"] = bool(out["authenticity_enforce"])
    for k in (
        "min_interval_seconds", "cadence_jitter", "backoff_base_seconds",
        "backoff_max_seconds", "usage_budget_fraction", "authenticity_threshold",
    ):
        out[k] = float(out[k])
    for k in (
        "affinity_ttl_seconds", "affinity_max_sessions", "client_cache_size",
        "backoff_max_retries", "session_ttl_seconds", "usage_window_seconds",
    ):
        out[k] = int(out[k])
    # session_key_fields stays a list
    if "session_key_fields" in raw and isinstance(raw["session_key_fields"], list):
        out["session_key_fields"] = list(raw["session_key_fields"])
    else:
        out["session_key_fields"] = [
            "metadata.session_id", "metadata.conversation_id",
            "metadata.user_id", "prompt_cache_key", "user",
        ]
    return out


def is_oauth_connection(conn: Any) -> bool:
    """True only when THIS connection carries an OAuth credential marker.

    Name-level detection (`is_oauth_provider`) is deliberately looser: it exists
    to decide whether session AFFINITY should pin a provider's pool. Egress
    CLIENT isolation is a stronger, per-credential decision, so it requires real
    evidence on the active row - an OAuth-marked lane registered under the name
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
    if isinstance(provider_config, dict) and provider_config.get("type") == "oauth":
        return True
    for conn in (provider_config or {}).get("connections", []) or []:
        if is_oauth_connection(conn):
            return True
    return False


def is_claude_provider(provider_name: str) -> bool:
    return (provider_name or "").strip().lower() in _CLAUDE_FAMILY


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
_SESSION_RR_CURSOR: Dict[str, int] = {}
_SESSION_RR_LOCK = threading.Lock()


def get_store() -> AffinityStore:
    return _STORE


def reset_store() -> None:
    """Test/config-reload hook: drop all bindings."""
    global _STORE
    _STORE = AffinityStore()
    with _SESSION_RR_LOCK:
        _SESSION_RR_CURSOR.clear()


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
        is_rebind = False
        if bound is not None and bound in by_index:
            if _is_healthy(breaker, provider_name, model_id, bound):
                return [by_index[bound]]
            # Sticky account tripped the breaker -> rebind once to a healthy peer.
            store.release(session_key, provider_name)
            is_rebind = True
            print(
                "[affinity] rebind provider=%s reason=breaker-open released_idx=%s"
                % (provider_name, bound),
                flush=True,
            )

        # New session under round_robin: rotate starting account across distinct sessions,
        # then pin the session to that chosen account for subsequent turns.
        if (
            not is_rebind
            and bound is None
            and isinstance(provider_config, dict)
            and bool(provider_config.get("round_robin"))
        ):
            healthy_eligible = [
                e for e in eligible
                if isinstance(e, dict)
                and e.get("index") is not None
                and _is_healthy(breaker, provider_name, model_id, e["index"])
            ]
            if healthy_eligible:
                with _SESSION_RR_LOCK:
                    cur = _SESSION_RR_CURSOR.get(provider_name, 0)
                    chosen = healthy_eligible[cur % len(healthy_eligible)]
                    _SESSION_RR_CURSOR[provider_name] = cur + 1
                store.bind(session_key, provider_name, chosen["index"])
                return [chosen]

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


# ===========================================================================
# Gap implementations G1-G6 (layered on top of the affinity primitives above)
# ===========================================================================


# ---------------------------------------------------------------------------
# G1 -- Cadence governor (per-account jittered min-interval)
# ---------------------------------------------------------------------------

class CadenceGovernor:
    """Per-account pacing: enforces a jittered minimum interval between sends.

    Kills the *metronome* fingerprint: identical inter-request gaps. Each
    account gets an independent lock + last-send timestamp; the wait is
    base * (1 + U(0, jitter)) so cadence carries human entropy. The wait is
    an asyncio sleep on the request task, never a thread block. Fail-open.
    """

    def __init__(self) -> None:
        self._locks: Dict[str, Any] = {}
        self._last: Dict[str, float] = {}
        self._guard = threading.Lock()

    def _lock_for(self, account: str):
        import asyncio
        with self._guard:
            lk = self._locks.get(account)
            if lk is None:
                lk = asyncio.Lock()
                self._locks[account] = lk
            return lk

    async def wait_turn(
        self,
        account: str,
        min_interval: float,
        jitter: float,
        sleep=None,
        monotonic=None,
    ) -> float:
        """Wait until this account may send again. Returns waited seconds.

        `sleep` / `monotonic` injectable for tests. Never raises; on any error
        returns 0.0 immediately (fail-open = no added latency).
        """
        try:
            import asyncio
            sleep = sleep or asyncio.sleep
            monotonic = monotonic or time.monotonic
            lock = self._lock_for(account)
            async with lock:
                now = monotonic()
                last = self._last.get(account)
                if last is None:
                    self._last[account] = now
                    return 0.0
                target = float(min_interval) * (1.0 + random.uniform(0.0, max(0.0, jitter)))
                gap = now - last
                wait = target - gap
                if wait > 0:
                    await sleep(wait)
                    now = monotonic()
                else:
                    wait = 0.0
                self._last[account] = now
                return wait
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# G2 -- Adaptive 429 backoff (Retry-After aware, jittered, no retry-storm)
# ---------------------------------------------------------------------------

class BackoffGovernor:
    """Per-account exponential backoff driven by upstream 429 signals.

    Distinct from the circuit breaker: the breaker trips a *sick lane*; this
    governor paces *re-attempts on a rate-limited but healthy account* so we
    never retry-storm. Hard cap on consecutive retries per account. Fail-open.
    """

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

    def _bucket(self, account: str) -> Dict[str, float]:
        return self._state.setdefault(account, {"retries": 0.0, "until": 0.0})

    def blocked_until(self, account: str) -> float:
        with self._lock:
            return self._bucket(account).get("until", 0.0)

    def should_retry(self, account: str, max_retries: int) -> bool:
        with self._lock:
            return self._bucket(account).get("retries", 0.0) < float(max_retries)

    def next_delay(
        self,
        account: str,
        base: float,
        cap: float,
        retry_after: Optional[float] = None,
    ) -> float:
        """Compute + commit the backoff delay for this account. Never raises."""
        try:
            with self._lock:
                b = self._bucket(account)
                n = b.get("retries", 0.0)
                if retry_after is not None and retry_after >= 0:
                    delay = float(retry_after)
                else:
                    delay = float(base) * (2.0 ** n)
                delay = min(float(cap), delay)
                delay = delay * (1.0 + random.uniform(0.0, 0.25))  # decorrelate
                b["retries"] = n + 1.0
                b["until"] = time.monotonic() + delay
                return delay
        except Exception:
            return float(base)

    def record_success(self, account: str) -> None:
        with self._lock:
            if account in self._state:
                self._state[account] = {"retries": 0.0, "until": 0.0}


# ---------------------------------------------------------------------------
# G3 -- Session-lane pinning (account <-> stable egress identity stickiness)
# ---------------------------------------------------------------------------

class SessionLaneStore:
    """Bounded TTL+LRU map: session_key -> (account_id, egress_proxy).

    The IP-level-lockout countermeasure: a session stays pinned to ONE account
    AND that account's egress identity, so we never present N accounts over one
    socket/IP within a session. In-memory by design.
    """

    def __init__(self, ttl_seconds: int = 1800, max_sessions: int = 512) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(1, int(max_sessions))
        self._data: "OrderedDict[str, Tuple[Tuple[str, Optional[str]], float]]" = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, session_key: str) -> Optional[Tuple[str, Optional[str]]]:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(session_key)
            if entry is None:
                return None
            lane, ts = entry
            if now - ts > self._ttl:
                self._data.pop(session_key, None)
                return None
            self._data.move_to_end(session_key)
            return lane

    def bind(self, session_key: str, account_id: str, egress_proxy: Optional[str]) -> None:
        with self._lock:
            self._data[session_key] = ((account_id, egress_proxy), time.monotonic())
            self._data.move_to_end(session_key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def release(self, session_key: str) -> None:
        with self._lock:
            self._data.pop(session_key, None)

    def __len__(self) -> int:  # pragma: no cover
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# G4 -- Claude session-lifecycle telemetry rhythm
# ---------------------------------------------------------------------------

class ClaudeSessionRhythm:
    """Mimic the Claude Code harness session lifecycle, not just its headers.

    Anthropic reads the *rhythm*: a session opens, keeps alive, sends turns
    with human think-time, and carries a stable session id. We track per-session
    turn count + last-turn time and expose the session header surface the real
    CLI emits, plus a think-time hint the dispatcher can sleep on. Fail-open.
    """

    SESSION_HEADER = "x-claude-code-session-id"

    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._sessions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, session_key: str) -> Dict[str, Any]:
        import uuid
        now = time.monotonic()
        s = self._sessions.get(session_key)
        if s is None or now - s.get("last", 0.0) > self._ttl:
            s = {"session_id": uuid.uuid4().hex, "turns": 0, "opened": now, "last": now}
            self._sessions[session_key] = s
        self._sessions.move_to_end(session_key)
        while len(self._sessions) > 512:
            self._sessions.popitem(last=False)
        return s

    def headers_for(self, session_key: str) -> Dict[str, str]:
        """Return the session-telemetry headers the dispatcher should merge."""
        try:
            with self._lock:
                s = self._get(session_key)
            return {self.SESSION_HEADER: s["session_id"]}
        except Exception:
            return {}

    def note_turn(self, session_key: str) -> float:
        """Record a turn; return suggested human think-time (s) before next turn."""
        try:
            with self._lock:
                s = self._get(session_key)
                now = time.monotonic()
                s["turns"] += 1
                s["last"] = now
            if s["turns"] <= 1:
                return 0.0
            return random.uniform(0.4, 1.6)
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# G5 -- Graded authenticity pre-send check (6-dimension score)
# ---------------------------------------------------------------------------

_AUTH_WEIGHTS = {
    "client_identity": 0.28,    # official CLI headers/UA/profile pinned
    "behavioral_cadence": 0.18,  # cadence governor engaged (not metronomic)
    "network_origin": 0.18,     # dedicated egress / stable account IP
    "account_graph": 0.12,      # session pinned to one account (no spray)
    "quota_shape": 0.12,        # usage budget not already blown
    "telemetry_session": 0.12,  # session lifecycle rhythm (Claude weighted)
}


def authenticity_score(
    provider_name: str,
    has_official_profile: bool,
    cadence_engaged: bool,
    has_stable_egress: bool,
    session_pinned: bool,
    usage_fraction: float,
    session_rhythm: bool,
) -> Tuple[float, Dict[str, float]]:
    """Score an outgoing request on the 6 anti-ban dimensions.

    Returns (score, breakdown). Fail-open: any internal error returns a
    perfect-neutral score so a scoring bug can never block traffic.
    """
    try:
        usage_ok = 1.0 if usage_fraction < 0.9 else max(0.0, 1.0 - (usage_fraction - 0.9) * 10.0)
        parts = {
            "client_identity": 1.0 if has_official_profile else 0.0,
            "behavioral_cadence": 1.0 if cadence_engaged else 0.0,
            "network_origin": 1.0 if has_stable_egress else 0.0,
            "account_graph": 1.0 if session_pinned else 0.0,
            "quota_shape": usage_ok,
            "telemetry_session": 1.0 if session_rhythm else 0.0,
        }
        score = sum(_AUTH_WEIGHTS[k] * parts[k] for k in _AUTH_WEIGHTS)
        return score, parts
    except Exception:
        return 1.0, {}


def authenticity_grade(score: float) -> str:
    if score >= 0.75:
        return "human"
    if score >= 0.5:
        return "suspicious"
    return "bot"


# ---------------------------------------------------------------------------
# G6 -- Per-account usage-budget tracker (Antigravity 5h sliding window)
# ---------------------------------------------------------------------------

_ONE_DAY = 86400.0
_SEVEN_DAYS = 7 * _ONE_DAY
_CONNECTION_USAGE: Dict[Tuple[str, Optional[int]], deque] = {}
_CONNECTION_USAGE_LOCK = threading.Lock()


def _prune_window(q: deque, now: float) -> None:
    cutoff = now - _SEVEN_DAYS
    while q and q[0][0] < cutoff:
        q.popleft()


def snapshot_connection_usage(provider_id: str, conn_index: Optional[int], now: Optional[float] = None) -> Dict[str, float]:
    now = time.monotonic() if now is None else now
    with _CONNECTION_USAGE_LOCK:
        q = _CONNECTION_USAGE.get((provider_id, conn_index), deque())
        _prune_window(q, now)
        reqs = sum(1 for ts, _ in q if ts >= now - _ONE_DAY)
        toks = sum(t for ts, t in q if ts >= now - _ONE_DAY)
    return {"requests_last_24h": reqs, "tokens_last_24h": toks}


class UsageBudgetTracker:
    """Sliding-window per-account token-spend tracker.

    Antigravity AI-Pro uses a 5h sliding window; the authentic pattern is to
    drain one account toward its window edge, then lane-switch -- not to
    round-robin-spray all accounts. We pace to a configurable fraction of the
    window and report spend fraction so the dispatcher can prefer the
    least-loaded account and never discover the limit via a 429. Fail-open.
    """

    def __init__(self, window_seconds: int = 18000) -> None:
        self._window = max(60, int(window_seconds))
        self._events: Dict[str, List[Tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def _prune(self, account: str, now: float) -> List[Tuple[float, float]]:
        evs = self._events.setdefault(account, [])
        cutoff = now - self._window
        i = 0
        while i < len(evs) and evs[i][0] < cutoff:
            i += 1
        if i:
            del evs[:i]
        return evs

    def record(self, account: str, tokens: float) -> None:
        try:
            with self._lock:
                now = time.monotonic()
                evs = self._prune(account, now)
                evs.append((now, float(tokens)))
        except Exception:
            pass

    def window_spend(self, account: str) -> float:
        try:
            with self._lock:
                now = time.monotonic()
                evs = self._prune(account, now)
                return sum(t for _, t in evs)
        except Exception:
            return 0.0

    def spend_fraction(self, account: str, budget_tokens: float) -> float:
        if budget_tokens <= 0:
            return 0.0
        return min(1.0, self.window_spend(account) / float(budget_tokens))

    def over_budget(self, account: str, budget_tokens: float, fraction: float) -> bool:
        try:
            return self.window_spend(account) >= float(budget_tokens) * float(fraction)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Process-global singletons for the gap governors
# ---------------------------------------------------------------------------

_CADENCE = CadenceGovernor()
_BACKOFF = BackoffGovernor()
_LANES = SessionLaneStore()
_CLAUDE_RHYTHM = ClaudeSessionRhythm()
_USAGE = UsageBudgetTracker()


def get_cadence() -> CadenceGovernor:
    return _CADENCE


def get_backoff() -> BackoffGovernor:
    return _BACKOFF


def get_lanes() -> SessionLaneStore:
    return _LANES


def get_claude_rhythm() -> ClaudeSessionRhythm:
    return _CLAUDE_RHYTHM


def get_usage() -> UsageBudgetTracker:
    return _USAGE


def reset_all() -> None:
    """Test/config-reload hook: drop ALL in-memory anti-ban state."""
    global _STORE, _CADENCE, _BACKOFF, _LANES, _CLAUDE_RHYTHM, _USAGE
    _STORE = AffinityStore()
    with _SESSION_RR_LOCK:
        _SESSION_RR_CURSOR.clear()
    _CADENCE = CadenceGovernor()
    _BACKOFF = BackoffGovernor()
    _LANES = SessionLaneStore()
    _CLAUDE_RHYTHM = ClaudeSessionRhythm()
    _USAGE = UsageBudgetTracker()
    with _CONNECTION_USAGE_LOCK:
        _CONNECTION_USAGE.clear()


# ---------------------------------------------------------------------------
# High-level composed API used by the dispatcher (one call per send)
# ---------------------------------------------------------------------------


async def govern_pre_send(
    provider_name: str,
    conn: Dict[str, Any],
    conn_index: Optional[int],
    session_key: Optional[str],
    config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """One composed pre-send gate: G1 cadence + G3 lane pin + G4 + G5.

    Returns a decision dict:
      {proceed, waited, grade, score, pinned_account, pinned_egress, headers}
    Fail-open: any internal error -> proceed=True with zero added latency.
    """
    decision = {
        "proceed": True, "waited": 0.0, "grade": "unknown", "score": 1.0,
        "pinned_account": None, "pinned_egress": None, "headers": {},
    }
    try:
        cfg = resolve_antiban_config(config)
        if not cfg["enabled"] or not is_oauth_provider(provider_name, None):
            # fall back to connection-marker detection for custom-named lanes
            if not (cfg["enabled"] and any(is_oauth_connection(c) for c in [conn])):
                return decision

        acct = account_identity(conn, conn_index)
        session_key = session_key or current_request_session_key()

        # G1 -- cadence governor (jittered min-interval)
        decision["waited"] = await _CADENCE.wait_turn(
            acct, cfg["min_interval_seconds"], cfg["cadence_jitter"],
        )

        # G3 -- session-lane pinning (account <-> egress identity)
        pinned_egress = conn.get("egress_proxy")
        if session_key:
            lane = _LANES.lookup(session_key)
            if lane is None:
                _LANES.bind(session_key, acct, pinned_egress)
            else:
                pinned_account, pinned_egress = lane
                decision["pinned_account"] = pinned_account
            decision["pinned_egress"] = pinned_egress

        # G4 -- Claude session rhythm headers
        if session_key and is_claude_provider(provider_name):
            decision["headers"] = _CLAUDE_RHYTHM.headers_for(session_key)
            _CLAUDE_RHYTHM.note_turn(session_key)

        # G5 -- graded authenticity pre-send
        score, _parts = authenticity_score(
            provider_name,
            has_official_profile=bool(conn.get("header_profile") or conn.get("_profile")),
            cadence_engaged=True,
            has_stable_egress=bool(pinned_egress) or bool(conn.get("proxy_bypass")),
            session_pinned=bool(session_key),
            usage_fraction=_USAGE.spend_fraction(acct, float(conn.get("_usage_budget_tokens") or 0.0)),
            session_rhythm=bool(decision["headers"]) or not is_claude_provider(provider_name),
        )
        decision["score"] = score
        decision["grade"] = authenticity_grade(score)
        if cfg["authenticity_enforce"] and score < cfg["authenticity_threshold"]:
            decision["proceed"] = False
            print(
                "[antiban] BLOCK provider=%s grade=%s score=%.2f acct=%s"
                % (provider_name, decision["grade"], score, acct),
                flush=True,
            )
        elif score < cfg["authenticity_threshold"]:
            print(
                "[antiban] warn provider=%s grade=%s score=%.2f acct=%s"
                % (provider_name, decision["grade"], score, acct),
                flush=True,
            )
        return decision
    except Exception as exc:
        print("[antiban] pre-send fail-open (%s)" % type(exc).__name__, flush=True)
        decision["proceed"] = True
        return decision


def govern_post_send(
    provider_name: str,
    conn: Dict[str, Any],
    conn_index: Optional[int],
    status_code: int,
    retry_after: Optional[float] = None,
    tokens_used: float = 0.0,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One composed post-send gate: G2 adaptive backoff + G6 usage tracking.

    Returns {retry, delay} for the dispatcher's retry loop. Fail-open.
    """
    out = {"retry": False, "delay": 0.0}
    try:
        with _CONNECTION_USAGE_LOCK:
            _now = time.monotonic()
            _q = _CONNECTION_USAGE.setdefault((provider_name, conn_index), deque())
            _prune_window(_q, _now)
            _q.append((_now, float(tokens_used)))
    except Exception:
        pass
    if status_code in (401, 402, 403, 429) or (isinstance(status_code, int) and status_code >= 500):
        try:
            _STORE.release(current_request_session_key(), provider_name)
        except Exception:
            pass
    try:
        cfg = resolve_antiban_config(config)
        if not cfg["enabled"]:
            return out
        if not (is_oauth_provider(provider_name, None) or is_oauth_connection(conn)):
            return out
        acct = account_identity(conn, conn_index)

        # G6 -- usage budget tracking
        if tokens_used > 0:
            _USAGE.record(acct, tokens_used)

        # G2 -- adaptive 429 backoff, no retry-storm
        if status_code == 429:
            if not _BACKOFF.should_retry(acct, cfg["backoff_max_retries"]):
                print(
                    "[antiban] 429 retry-storm guard tripped provider=%s acct=%s"
                    % (provider_name, acct),
                    flush=True,
                )
                return out
            delay = _BACKOFF.next_delay(
                acct, cfg["backoff_base_seconds"], cfg["backoff_max_seconds"],
                retry_after=retry_after,
            )
            out["retry"] = True
            out["delay"] = delay
        elif 200 <= status_code < 300:
            _BACKOFF.record_success(acct)
        return out
    except Exception:
        return out