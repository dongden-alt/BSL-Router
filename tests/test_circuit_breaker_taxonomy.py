"""Regression tests for the circuit-breaker failure taxonomy (L1 fix).

Guards against client-side request faults (oversized prompt, malformed body)
tripping healthy connections. Client errors must classify as 'client_error'
and be treated as non-penalizing, while genuine rate limits still trip.
"""
import pytest
from app.circuit_breaker import _classify_for_breaker, CircuitBreaker


@pytest.mark.parametrize("status,msg,expected", [
    # Client-side faults — must NOT penalize the connection.
    (400, "This model maximum context length is 200000 tokens, however you requested 250000", "client_error"),
    (200, "token limit exceeded", "client_error"),
    (200, "too many tokens in prompt", "client_error"),
    (400, "invalid_request_error: messages too long", "client_error"),
    (500, "ascii codec cant encode", "client_error"),
    (413, "payload too large", "client_error"),
    (422, "unprocessable entity", "client_error"),
    # Genuine rate limits — must still trip immediately.
    (429, "Too Many Requests", "rate_limit"),
    (200, "rate limit reached", "rate_limit"),
    (200, "rate limit exceeded", "rate_limit"),  # collision guard vs client_error
    (200, "quota", "rate_limit"),
    # TPM / token-per-minute rate limits — must NOT be swallowed by client_error.
    (429, "You have hit your token limit per minute", "rate_limit"),   # literal 429 guard
    (200, "rate limit reached: too many tokens per minute", "rate_limit"),
    (429, "too many tokens per minute", "rate_limit"),
    (200, "TPM quota exceeded", "rate_limit"),
    # Hard status gates — 401/403 outrank any message markers.
    (403, "token limit exceeded in forbidden request", "auth"),  # 403 gate
    (401, "too many tokens in unauthorized request", "auth"),    # 401 gate
    # Other classes unchanged.
    (503, "service unavailable", "server_error"),
    (401, "unauthorized", "auth"),
    (0, "client_disconnected", "non_penalizing"),
    (200, None, "success"),
])
def test_classification(status, msg, expected):
    assert _classify_for_breaker(status, msg) == expected


def _breaker():
    return CircuitBreaker({"circuit_breaker": {"enabled": True, "failure_threshold": 3, "recovery_timeout": 30}})


def test_client_error_never_opens_connection():
    """An oversized-prompt 400 must never OPEN a healthy connection."""
    cb = _breaker()
    for _ in range(10):
        cb.record_outcome("openai", "gpt-5.5", 0, 400, "context length exceeded")
    is_open, _ = cb.is_open("openai", "gpt-5.5", 0)
    assert is_open is False


def test_rate_limit_opens_immediately():
    """A single 429 must OPEN the connection on the first hit."""
    cb = _breaker()
    cb.record_outcome("openai", "gpt-5.5", 0, 429, "Too Many Requests")
    is_open, remaining = cb.is_open("openai", "gpt-5.5", 0)
    assert is_open is True
    assert remaining is not None and remaining > 0


def test_client_error_does_not_reset_real_failures():
    """A client_error is neutral: it neither increments nor resets the streak."""
    cb = _breaker()
    cb.record_outcome("openai", "gpt-5.5", 0, 503, "service unavailable")
    cb.record_outcome("openai", "gpt-5.5", 0, 503, "service unavailable")
    # Neutral event in the middle must not reset the 2 real failures.
    cb.record_outcome("openai", "gpt-5.5", 0, 400, "context length exceeded")
    cb.record_outcome("openai", "gpt-5.5", 0, 503, "service unavailable")
    is_open, _ = cb.is_open("openai", "gpt-5.5", 0)
    assert is_open is True  # 3 real 5xx failures reached threshold


def test_single_stall_does_not_immediately_open():
    """A single stream stall should increment but not OPEN (unlike rate_limit).

    Production path: watchdog sets stats['error']='stream_stall' → log_request
    → record_outcome(200, 'stream_stall') → classifier returns 'server_error'
    → increments once.
    """
    cb = _breaker()
    cb.record_outcome("openai", "gpt-5.5", 0, 200, "stream_stall")
    is_open, _ = cb.is_open("openai", "gpt-5.5", 0)
    assert is_open is False  # threshold=3, only 1 stall


def test_stall_at_threshold_opens():
    """3 consecutive stalls should OPEN the connection (threshold reached)."""
    cb = _breaker()
    for _ in range(3):
        cb.record_outcome("openai", "gpt-5.5", 0, 200, "stream_stall")
    is_open, remaining = cb.is_open("openai", "gpt-5.5", 0)
    assert is_open is True
    assert remaining is not None and remaining > 0


def test_stall_classified_as_server_error():
    """The classifier must recognize 'stream_stall' as server_error, not
    unknown_error. This is the regression guard for the double-counting bug
    where record_outcome would have incremented a SECOND time via the
    unknown_error fallback."""
    assert _classify_for_breaker(200, "stream_stall") == "server_error"


def test_stall_no_double_counting():
    """Regression: record_outcome(200, 'stream_stall') must increment exactly
    once. Before the fix, _classify_for_breaker returned 'unknown_error' for
    'stream_stall', which also increments — causing 1 stall = 2 failures."""
    cb = _breaker()
    cb.record_outcome("openai", "gpt-5.5", 0, 200, "stream_stall")
    key = cb._key("openai", "gpt-5.5", 0)
    assert cb.state[key]["consecutive_failures"] == 1  # exactly 1, not 2


def test_stall_last_error_type():
    """record_outcome with stream_stall sets last_error_type to 'server_error'
    (the classification), preserving breaker telemetry consistency."""
    cb = _breaker()
    cb.record_outcome("openai", "gpt-5.5", 0, 200, "stream_stall")
    key = cb._key("openai", "gpt-5.5", 0)
    assert cb.state[key]["last_error_type"] == "server_error"
