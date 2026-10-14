"""Tests for the AntiStop continuation-send retry (2026-10-15).

Part B of the 502 force-stop hardening: the post-loop continuation splice
re-sends to the SAME upstream. Before this fix a single transient 5xx (or
transport error) on that send died via the fail-open handler and surfaced a
client-visible 502 after partial content had already been delivered. The
module-level helper app.main._open_continuation_with_retry now retries
transient failures up to 3 total attempts with bounded backoff; 4xx is never
retried; final failure re-raises into the existing per-site fail-open path so
stats keep reporting the truthful 502.

PURE unit tests - no network, no subprocess. asyncio.run drives the async
helper with fake send functions (same pattern as
test_anthropic_to_openai_idle.py). Backoff is neutralized by monkeypatching
app.main.CONTINUATION_SEND_BACKOFF_BASE to 0 so the suite stays fast.
"""

import asyncio

import httpx
import pytest

from app import main as _main


def _status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://upstream.test/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"upstream {status}", request=req, response=resp)


class _FakeResp:
    """Minimal stand-in for the live httpx streaming response."""

    def __init__(self, status: int):
        self.status_code = status
        self.closed = False
        self._err = _status_error(status) if status >= 400 else None

    def raise_for_status(self):
        if self._err is not None:
            raise self._err

    async def aclose(self):
        self.closed = True


def _make_send(script):
    """Build a fake send_fn consuming a scripted list of outcomes.

    Each entry is either an int status (returned as a _FakeResp) or an
    Exception instance (raised directly by send_fn itself). Records every
    (req, payload) call so tests can assert the exact attempt count.
    """
    calls = []

    async def _send(stream_req=None, stream_payload=None):
        calls.append((stream_req, stream_payload))
        outcome = script[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResp(outcome)

    return _send, calls


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(_main, "CONTINUATION_SEND_BACKOFF_BASE", 0.0)


def _build_req(payload):
    return ("REQ", payload)


# --- _continuation_send_retryable classification ---------------------------


def test_retryable_5xx_true():
    assert _main._continuation_send_retryable(_status_error(500)) is True
    assert _main._continuation_send_retryable(_status_error(502)) is True
    assert _main._continuation_send_retryable(_status_error(503)) is True


def test_retryable_4xx_false():
    # 4xx is deterministic - NEVER retried, even though HTTPStatusError is a
    # subclass of httpx.HTTPError (a member of UPSTREAM_TRANSPORT_ERRORS).
    assert _main._continuation_send_retryable(_status_error(400)) is False
    assert _main._continuation_send_retryable(_status_error(401)) is False
    assert _main._continuation_send_retryable(_status_error(429)) is False


def test_retryable_transport_and_timeout_true():
    assert _main._continuation_send_retryable(httpx.ConnectError("boom")) is True
    assert _main._continuation_send_retryable(httpx.ReadTimeout("boom")) is True
    assert _main._continuation_send_retryable(TimeoutError("header wait")) is True
    assert _main._continuation_send_retryable(asyncio.TimeoutError()) is True


def test_retryable_unrelated_error_false():
    assert _main._continuation_send_retryable(ValueError("nope")) is False


# --- _open_continuation_with_retry behavior --------------------------------


def test_retry_succeeds_after_transient_5xx():
    """502 then 200: splice completes with 200 - no terminal 502 surfaces."""
    send, calls = _make_send([502, 200])
    resp = asyncio.run(
        _main._open_continuation_with_retry(send, _build_req, {"p": 1}, "openai-cont")
    )
    assert resp.status_code == 200
    assert len(calls) == 2


def test_retry_failed_attempt_response_is_closed():
    """The failed attempt's response is aclosed before the next send; the
    winning response stays open for the caller to iterate."""
    bad = _FakeResp(502)
    good = _FakeResp(200)
    responses = [bad, good]
    calls = []

    async def send(stream_req=None, stream_payload=None):
        calls.append((stream_req, stream_payload))
        return responses[len(calls) - 1]

    resp = asyncio.run(
        _main._open_continuation_with_retry(send, _build_req, {}, "gemini-cont")
    )
    assert resp is good
    assert bad.closed is True
    assert good.closed is False
    assert len(calls) == 2


def test_retry_never_retries_4xx():
    """400: exactly ONE send attempt, error propagates immediately."""
    send, calls = _make_send([400, 200, 200])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            _main._open_continuation_with_retry(send, _build_req, {}, "openai-cont")
        )
    assert len(calls) == 1


def test_retry_exhausts_and_raises_last_error():
    """All attempts 502: exactly MAX attempts, last error re-raised so the
    per-site fail-open keeps reporting the truthful 502."""
    send, calls = _make_send([502] * (_main.CONTINUATION_SEND_MAX_ATTEMPTS + 1))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(
            _main._open_continuation_with_retry(send, _build_req, {}, "gemini-cont")
        )
    assert excinfo.value.response.status_code == 502
    assert len(calls) == _main.CONTINUATION_SEND_MAX_ATTEMPTS


def test_retry_transport_error_then_success():
    """A transport-level death (no HTTP response at all) is also retried."""
    send, calls = _make_send([httpx.ConnectError("dropped"), 200])
    resp = asyncio.run(
        _main._open_continuation_with_retry(send, _build_req, {}, "anthropic-egress-cont")
    )
    assert resp.status_code == 200
    assert len(calls) == 2


def test_retry_transport_error_exhausts():
    """Transport death on every attempt: MAX attempts, then the last error
    propagates (fail-open)."""
    send, calls = _make_send(
        [httpx.ConnectError("dropped")] * (_main.CONTINUATION_SEND_MAX_ATTEMPTS + 1)
    )
    with pytest.raises(httpx.ConnectError):
        asyncio.run(
            _main._open_continuation_with_retry(send, _build_req, {}, "openai-cont")
        )
    assert len(calls) == _main.CONTINUATION_SEND_MAX_ATTEMPTS
