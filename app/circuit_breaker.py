"""
Connection-Level Circuit Breaker — Per-account health isolation.

Complements the existing model-level ErrorPrevention system. ErrorPrevention
bans at the (provider, model) granularity; this module tracks health at the
finer (provider, model, connection_index) granularity so that one bad API key
or rate-limited account does not poison the entire provider+model pool.

States (per connection key):
  CLOSED    — healthy, traffic flows normally
  OPEN      — unhealthy, connection skipped during selection
  HALF_OPEN — recovery probe in-flight; one request allowed to test

Taxonomy (non-penalizing events NEVER increment failure count):
  - client_disconnected: user aborted (tab close, IDE exit) — not an upstream fault
  - client_error (400/413/422): oversized prompt, malformed body — caller's fault
  - success (HTTP 200 + out_tokens > 0): resets failure count
  - upstream errors (5xx, timeout, network): increment failure count
  - rate_limit (429): immediate OPEN (like ErrorPrevention's cooldown)
  - stream_stall: 200 connected but no data within stream_stall_timeout — increment
    (wired via _stall_watchdog wrapper in main.py, catches hung accounts that
    accept connections but never send body data)

All operations are fail-open: any exception in the breaker itself must never
break the proxy path. The breaker is an optimization, not a gate.
"""
import time
from typing import Dict, Any, Optional, Tuple, List

# ── Non-penalizing error markers ────────────────────────────────────────
# These error strings in stats["error"] must NEVER count toward breaker
# failure counts. They represent client-side behavior, not upstream faults.
NON_PENALIZING_MARKERS = (
    "client_disconnected",
)

# ── Error classification ────────────────────────────────────────────────
# Genuine rate-limit / quota-exhaustion signals ONLY. These trip the
# connection immediately (an account-level health problem). Deliberately
# EXCLUDES bare "exceeded" (which matches client "context length exceeded")
# and "ascii codec" (an encoding fault, not a rate limit) — both of those
# are client-side faults handled by CLIENT_ERROR_MARKERS below. In a
# connection breaker an immediate OPEN is unforgiving, so a caller sending
# an oversized prompt must never isolate a healthy account.
RATE_LIMIT_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests",
    "quota", "1302",
    "\u901f\u5ea6\u9650\u5236", "\u8bf7\u6c42\u9891\u7387",
)

# Client-side request faults (oversized prompt, malformed body, bad params).
# These are the CALLER's problem, not connection health — they must NEVER
# increment the failure count. Kept distinct from client_disconnected only
# for clearer telemetry; both are treated as non-penalizing downstream.
CLIENT_ERROR_MARKERS = (
    "context length", "context_length", "maximum context",
    "token limit", "max_tokens", "too many tokens",
    "too long", "request too large", "payload too large",
    "string too long", "invalid_request_error", "ascii codec",
)

AUTH_MARKERS = (
    "401", "403", "unauthorized", "forbidden", "authentication",
    # Billing/account issues — the account is dead, not the request.
    # Without these, a 400 Arrearage falls through to client_error
    # (non-penalizing) and the same dead connection is retried on every
    # request, wasting 60-86s per stream attempt before failing.
    "arrearage", "access denied", "account is in good standing",
    "insufficient balance", "insufficient_balance", "out of credit",
    "payment required", "billing", "unpaid", "account suspended",
    "account_disabled", "no credit",
)

SERVER_ERROR_MARKERS = (
    "500", "502", "503", "504", "internal server error",
    "bad gateway", "service unavailable", "gateway timeout",
    "524", "cloudflare",
)

TIMEOUT_MARKERS = (
    "timeout", "timed out", "time out",
)


def _classify_for_breaker(status_code: int, error_msg: Optional[str]) -> str:
    """Classify an outcome for breaker accounting.

    Returns one of: 'success', 'non_penalizing', 'client_error',
    'rate_limit', 'auth', 'server_error', 'timeout', 'network',
    'unknown_error'.

    Precedence (highest first):
      1. Hard status codes  — 429->rate_limit, 401/403->auth (outrank fuzzy text)
      2. client_disconnected — non-penalizing
      3. Token-rate co-occurrence — rate signal + token/quota/request words
      4. Client-error markers — oversized prompt, malformed body (non-penalizing)
      5. Generic rate-limit text — "quota" without 429 status
      6. Auth / timeout / server-error text markers
      7. Status-code fallback — 5xx, 400/413/422, network(0), success(200)
    """
    msg = (error_msg or "").lower()

    # 1. Hard status-code gates — outrank fuzzy message matching.
    #    A 429 is NEVER client-fixable; 401/403 are NEVER caused by prompt size.
    if status_code == 429:
        return "rate_limit"
    if status_code in (401, 403):
        return "auth"

    # 2. Non-penalizing: client disconnects are never the upstream's fault.
    for marker in NON_PENALIZING_MARKERS:
        if marker in msg:
            return "non_penalizing"

    # 2b. Stream stall: upstream accepted connection (200) but sent no data
    #     within the stall timeout. Classified as server_error — the
    #     connection is unhealthy, not the request.
    if "stream_stall" in msg:
        return "server_error"

    # 3. Token-rate-limit co-occurrence guard. Handles in-band proxy errors
    #    where status is unreliable (200/0) but the message carries a genuine
    #    rate/quota signal. Must fire BEFORE client_error so TPM limits are
    #    not swallowed by "too many tokens" / "token limit" markers.
    _has_rate_signal = (
        "rate limit" in msg or "rate_limit" in msg
        or "per minute" in msg or "per-minute" in msg
        or "tpm" in msg or "rpm" in msg
    )
    _has_quota_subject = "token" in msg or "quota" in msg or "request" in msg
    if _has_rate_signal and _has_quota_subject:
        return "rate_limit"

    # 4. Client-side request faults (oversized prompt, malformed body). Checked
    #    after status gates and token-rate guard so only genuine client faults
    #    land here. Non-penalizing: the caller must fix the request.
    for marker in CLIENT_ERROR_MARKERS:
        if marker in msg:
            return "client_error"

    # 5. Generic rate-limit text markers (no status-code twin — 429 handled
    #    above). Covers "quota", "too many requests" without a 429 status.
    for marker in RATE_LIMIT_MARKERS:
        if marker in msg:
            return "rate_limit"

    # 6. Auth text markers (non-401/403 fallback).
    for marker in AUTH_MARKERS:
        if marker in msg:
            return "auth"

    # 7. Timeout.
    for marker in TIMEOUT_MARKERS:
        if marker in msg:
            return "timeout"

    # 8. Server errors.
    for marker in SERVER_ERROR_MARKERS:
        if marker in msg or marker in str(status_code):
            return "server_error"

    # 9. Status-code fallback (text markers exhausted).
    if 500 <= status_code < 600:
        return "server_error"
    if status_code in (400, 413, 422):
        return "client_error"

    # Network-level (no status code, connection refused / DNS / etc.)
    if status_code == 0 and msg:
        return "network"

    if status_code == 200 and not error_msg:
        return "success"

    return "unknown_error"


class CircuitBreaker:
    """
    Manages per-connection circuit breaker state.

    State key: "{provider}/{model}/{connection_index}"

    State structure (in-memory, NOT persisted — connection health is
    ephemeral and should re-probe on restart):
    {
        "state": "CLOSED" | "OPEN" | "HALF_OPEN",
        "consecutive_failures": int,
        "last_failure_time": float,
        "opened_at": float,
        "open_until": float,       # CLOSED when in HALF_OPEN probe
        "last_success_time": float,
        "total_failures": int,
        "total_successes": int,
        "last_error_type": str,
    }
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.state: Dict[str, Dict[str, Any]] = {}

    @property
    def settings(self) -> Dict[str, Any]:
        """Live view of the circuit_breaker section.

        Deliberately NOT cached at construction. This used to be
        `self.settings = config.get("circuit_breaker", {})`, which froze the
        sub-dict at startup: saving new breaker settings from the admin panel
        (POST /api/config) rebuilt the top-level config, but the breaker kept
        reading the sub-dict captured on the original one. Toggling `enabled`
        or changing a threshold appeared to save (it did persist to
        config.yaml) yet had no effect until a full restart.
        """
        return self.config.get("circuit_breaker", {}) or {}

    def reconfigure(self, config: Dict[str, Any]) -> None:
        """Point the breaker at a replacement config dict.

        Accumulated per-connection health in self.state is PRESERVED on
        purpose: editing an unrelated config field must not silently clear
        every OPEN connection and re-admit traffic to accounts already known
        to be failing.
        """
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.settings.get("enabled", False)

    @property
    def failure_threshold(self) -> int:
        """Consecutive failures before OPEN."""
        return int(self.settings.get("failure_threshold", 3))

    @property
    def recovery_timeout(self) -> int:
        """Seconds in OPEN before transitioning to HALF_OPEN for a probe."""
        return int(self.settings.get("recovery_timeout", 30))

    @property
    def stream_stall_timeout(self) -> int:
        """Seconds without TTFT before a stream is declared stalled."""
        return int(self.settings.get("stream_stall_timeout", 30))

    def _key(self, provider: str, model: str, conn_index: int) -> str:
        return f"{provider}/{model}/{conn_index}"

    def _get_or_init(self, key: str) -> Dict[str, Any]:
        if key not in self.state:
            self.state[key] = {
                "state": "CLOSED",
                "consecutive_failures": 0,
                "last_failure_time": 0.0,
                "opened_at": 0.0,
                "open_until": 0.0,
                "last_success_time": 0.0,
                "total_failures": 0,
                "total_successes": 0,
                "last_error_type": None,
            }
        return self.state[key]

    def is_open(
        self, provider: str, model: str, conn_index: int
    ) -> Tuple[bool, Optional[float]]:
        """Check if a connection is currently OPEN (skip during selection).

        Returns:
            (is_open, seconds_remaining)
            — is_open=True means the connection should be SKIPPED.
            — HALF_OPEN returns (False, None) so the probe request can flow.
        """
        if not self.enabled:
            return False, None

        key = self._key(provider, model, conn_index)
        entry = self.state.get(key)
        if entry is None:
            return False, None

        now = time.time()
        current = entry.get("state", "CLOSED")

        if current == "OPEN":
            open_until = entry.get("open_until", 0.0)
            if now >= open_until:
                # Transition OPEN → HALF_OPEN: allow one probe request.
                entry["state"] = "HALF_OPEN"
                return False, None
            return True, open_until - now

        # CLOSED or HALF_OPEN: traffic flows.
        return False, None

    def filter_healthy_connections(
        self,
        provider: str,
        model: str,
        connections: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Filter out OPEN connections from the selection pool.

        If ALL connections are OPEN, returns the full list (least-recently-failed
        first) so the proxy never deadlocks. A stuck proxy is worse than retrying
        a possibly-bad connection.
        """
        if not self.enabled or not connections:
            return connections

        healthy = []
        for entry in connections:
            idx = entry["index"]
            conn = entry["conn"]
            is_open, remaining = self.is_open(provider, model, idx)
            if not is_open:
                healthy.append(entry)

        if healthy:
            return healthy

        # All OPEN — fall back to least-recently-failed to avoid deadlock.
        # Sort by last_failure_time ascending (oldest failure = least suspect).
        def _sort_key(entry):
            key = self._key(provider, model, entry["index"])
            s = self.state.get(key, {})
            return s.get("last_failure_time", 0.0)

        print(
            f"[CircuitBreaker] All {len(connections)} connections OPEN for "
            f"{provider}/{model} — falling back to least-recently-failed "
            f"(avoiding deadlock)",
            flush=True,
        )
        return sorted(connections, key=_sort_key)

    def record_outcome(
        self,
        provider: str,
        model: str,
        conn_index: int,
        status_code: int,
        error_msg: Optional[str] = None,
        out_tokens: int = 0,
    ):
        """Record a request outcome and update breaker state.

        Call AFTER the request completes (or fails). Fail-open: any internal
        exception is swallowed to protect the proxy path.

        Classification rules:
        - success (200, no error, OR out_tokens > 0): reset to CLOSED
        - non_penalizing (client_disconnected): no change (neutral)
        - client_error (400/413/422, oversized prompt): no change (neutral)
        - rate_limit: immediate OPEN
        - other errors: increment; OPEN if threshold reached
        """
        if not self.enabled:
            return

        try:
            classification = _classify_for_breaker(status_code, error_msg)

            # Non-penalizing: don't touch the breaker. Both a client abort
            # and a client-side request fault leave connection health intact.
            if classification in ("non_penalizing", "client_error"):
                return

            key = self._key(provider, model, conn_index)
            entry = self._get_or_init(key)
            now = time.time()

            # Success (or partial success with tokens): reset to CLOSED.
            if classification == "success":
                entry["state"] = "CLOSED"
                entry["consecutive_failures"] = 0
                entry["last_success_time"] = now
                entry["total_successes"] += 1
                # If this was a HALF_OPEN probe and it succeeded, the CLOSED
                # transition above already handles recovery.
                return

            # Rate limit OR auth: immediate OPEN (account is dead/unusable).
            # Auth errors (401/403/Arrearage/billing) are deterministic — the
            # same account will reject every retry. Immediate OPEN prevents
            # threshold-1 wasted requests waiting 60-86s on a dead account.
            if classification in ("rate_limit", "auth"):
                entry["consecutive_failures"] += 1
                entry["total_failures"] += 1
                entry["last_failure_time"] = now
                entry["last_error_type"] = classification
                entry["state"] = "OPEN"
                entry["opened_at"] = now
                entry["open_until"] = now + self.recovery_timeout
                print(
                    f"[CircuitBreaker] OPEN ({classification}) {key} for "
                    f"{self.recovery_timeout}s",
                    flush=True,
                )
                return

            # Other failures: increment, maybe OPEN.
            entry["consecutive_failures"] += 1
            entry["total_failures"] += 1
            entry["last_failure_time"] = now
            entry["last_error_type"] = classification

            # HALF_OPEN probe failed: re-OPEN.
            if entry.get("state") == "HALF_OPEN":
                entry["state"] = "OPEN"
                entry["opened_at"] = now
                entry["open_until"] = now + self.recovery_timeout
                print(
                    f"[CircuitBreaker] RE-OPEN (HALF_OPEN probe failed, "
                    f"{classification}) {key} for {self.recovery_timeout}s",
                    flush=True,
                )
                return

            # CLOSED → OPEN when threshold reached.
            if (
                entry.get("state") == "CLOSED"
                and entry["consecutive_failures"] >= self.failure_threshold
            ):
                entry["state"] = "OPEN"
                entry["opened_at"] = now
                entry["open_until"] = now + self.recovery_timeout
                print(
                    f"[CircuitBreaker] OPEN ({entry['consecutive_failures']} "
                    f"consecutive {classification}) {key} for "
                    f"{self.recovery_timeout}s",
                    flush=True,
                )

        except Exception as e:
            # Fail-open: breaker must never break the proxy.
            print(f"[CircuitBreaker] record_outcome error (non-blocking): {e}", flush=True)

    def get_health_summary(self) -> List[Dict[str, Any]]:
        """Get health status of all tracked connections (for dashboard)."""
        now = time.time()
        summary = []
        for key, entry in self.state.items():
            parts = key.rsplit("/", 2)
            if len(parts) != 3:
                continue
            provider, model, conn_idx = parts
            current_state = entry.get("state", "CLOSED")

            # Refresh OPEN → HALF_OPEN if timeout expired.
            if current_state == "OPEN" and now >= entry.get("open_until", 0):
                current_state = "HALF_OPEN"

            remaining = None
            if current_state == "OPEN":
                remaining = max(0, entry.get("open_until", 0) - now)

            summary.append({
                "provider": provider,
                "model": model,
                "connection_index": int(conn_idx),
                "state": current_state,
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "total_failures": entry.get("total_failures", 0),
                "total_successes": entry.get("total_successes", 0),
                "remaining_seconds": remaining,
                "last_error_type": entry.get("last_error_type"),
                "last_failure_time": entry.get("last_failure_time"),
                "last_success_time": entry.get("last_success_time"),
            })
        return summary

    def reset_all(self):
        """Reset all connections to CLOSED (admin action)."""
        for entry in self.state.values():
            entry["state"] = "CLOSED"
            entry["consecutive_failures"] = 0
            entry["open_until"] = 0.0


# ── Module-level singleton ──────────────────────────────────────────────
# Instantiated once at import time and reconfigured when config reloads.
_breaker: Optional[CircuitBreaker] = None


def init_breaker(config: Dict[str, Any]) -> CircuitBreaker:
    """Initialize the global circuit breaker from config (fresh state).

    Called from load_config() at startup. Use reconfigure_breaker() instead
    when config changes at runtime, so health state survives the swap.
    """
    global _breaker
    _breaker = CircuitBreaker(config)
    return _breaker


def reconfigure_breaker(config: Dict[str, Any]) -> None:
    """Re-point the existing breaker at a replacement config dict.

    No-op when the breaker was never initialized. Fail-open: the breaker is
    an optimization, so a problem here must never break the proxy path.
    """
    try:
        if _breaker is not None:
            _breaker.reconfigure(config)
    except Exception as exc:  # pragma: no cover - defensive, fail-open
        print(f"[CircuitBreaker] reconfigure failed (non-blocking): {exc}", flush=True)


def get_breaker() -> Optional[CircuitBreaker]:
    """Get the global circuit breaker instance (or None if not initialized)."""
    return _breaker

