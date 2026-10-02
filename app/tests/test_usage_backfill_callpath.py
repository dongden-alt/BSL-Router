"""CALL-PATH regression for the MintRouter in/out = 0/0 usage bug (2026-09-29).

Root cause (spec .brain/cc_tasks/task-bug1-usage-0-0/spec.md): the
StreamTruncationDetector is Qwen-scoped -- `enabled` was overloaded as BOTH the
truncation/splice gate AND the text-accumulation gate. For non-Qwen providers
(mintrouter/*) `enabled` is False, so `feed()` early-returned and
`partial_text` stayed "" forever. `_backfill_missing_usage` then early-returned
on the empty-delivery guard, leaving In:0/Out:0 for a HEALTHY stream. Two prior
"fixes" were falsely declared done because their tests called the helper
directly (test_gemini_egress_idle.py) -- they validated the estimator in
isolation and never exercised the call path that FEEDS it.

Fix (a)-(e): split the detector into `enabled` (splice gate, Qwen-unchanged)
vs `accumulate` (text tap, decoupled, on for every stream); construct it in
main.py with `accumulate=is_stream`; add feed+backfill to the Anthropic-egress
lanes that lacked it.

These tests exercise the REAL call path -- the actual egress generators inside
_process_chat_completion -- via the FastAPI route handlers (antigravity_generate
for the Gemini lane, chat_completions for the OpenAI _pump lane, anthropic_messages
for the convert_egress lane), driven by a fake streaming client whose upstream
emits content but NO terminal usage frame. The regression signal is: after the
stream drains, the usage backfill MUST have populated stats so the captured
obs.log_request shows in_tokens>0 AND out_tokens>0. Before the fix, partial_text
was "" -> backfill early-returned -> 0/0 persisted.

No live router is touched (AGENTS.md S1): no network, no :6969, in-process only.
Verified under both CI legs (.venv311 Py3.11 and .venv312 Py3.12).
"""

import asyncio
import json
import os
import sys

import httpx
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main
import app.observability as obs
from app.middleware.quality import StreamTruncationDetector


# ---------------------------------------------------------------------------
# Fakes: a streaming client whose upstream emits content deltas but drops the
# terminal usage object (the exact MintRouter defect). is_qwen is False for the
# chosen provider/model so the splice gate stays OFF -- accumulation must still
# flow (the fix under test).
# ---------------------------------------------------------------------------


def _openai_content_chunks(text_pieces):
    """OpenAI chat.completion.chunk SSE frames with content but NO usage."""
    out = []
    for piece in text_pieces:
        out.append(
            b'data: {"id":"c","object":"chat.completion.chunk","model":"fake-model",'
            b'"choices":[{"index":0,"delta":{"content":'
            + json.dumps(piece).encode("utf-8")
            + b'},"finish_reason":null}]}\n\n'
        )
    # Terminal stop frame -- deliberately WITHOUT a usage object (MintRouter).
    out.append(
        b'data: {"id":"c","object":"chat.completion.chunk","model":"fake-model",'
        b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    out.append(b"data: [DONE]\n\n")
    return out


def _anthropic_content_chunks(text_pieces):
    """Anthropic Messages SSE frames with text deltas but NO usage."""
    out = [
        (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"m","type":"message",'
            b'"role":"assistant","model":"fake-model","content":[],"stop_reason":null,'
            b'"usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
        ),
        (
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
        ),
    ]
    for piece in text_pieces:
        out.append(
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":'
            + json.dumps(piece).encode("utf-8")
            + b'}}\n\n'
        )
    # message_delta deliberately WITHOUT output_tokens usage (MintRouter).
    out.append(
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )
    out.append(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return out


class _RawStream(httpx.AsyncByteStream):
    """Yield a fixed list of bytes chunks then close. Never blocks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    async def __aiter__(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


class _FakeStreamingClient:
    """httpx-like client whose send() returns a 200 streaming response with the
    canned upstream frames. Captures nothing -- the regression signal is read
    from the captured obs.log_request call."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.sent = False

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.sent = True
        return httpx.Response(200, request=request, stream=_RawStream(self._chunks))


class _CaptureLogger:
    """Stand-in for obs.log_request that records every call's kwargs.

    The egress generators call obs.log_request exactly once at stream end (the
    END site) with in_tokens/out_tokens read from stats. After the fix, those
    are non-zero for a content-but-no-usage upstream; before the fix they were
    0/0 because the starved backfill never ran.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return None

    # Some call sites use positional args (legacy direct-log paths). Accept both.
    def log_request(self, *args, **kwargs):
        self.calls.append(kwargs)
        return None


def _config_openai_provider(provider_key="fake", model="fake-model"):
    """A single OpenAI-format provider so is_qwen is False (the regression
    precondition) and the slash-form `fake/fake-model` resolves cleanly."""
    return {
        "providers": {
            provider_key: {
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://fake.invalid"}],
                "models": [{"id": model, "enabled": True}],
            }
        },
        "aliases": {},
        "combos": [],
        "tools": {},
        "settings": {"combo_infinite_retry": False},
    }


def _config_anthropic_provider(provider_key="fakeanthro", model="fake-model"):
    """A single Anthropic-format upstream provider (convert_anthropic_to_openai
    and Anthropic->Anthropic passthrough lanes)."""
    return {
        "providers": {
            provider_key: {
                "format": "anthropic",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://fake.invalid"}],
                "models": [{"id": model, "enabled": True}],
            }
        },
        "aliases": {},
        "combos": [],
        "tools": {},
        "settings": {"combo_infinite_retry": False},
    }


def _block_forensics(monkeypatch, logger):
    """Suppress forensic file writes and route END logging through the capture
    logger so the regression signal (in/out tokens) is observable in-process."""
    monkeypatch.setattr(main.obs, "log_request", logger)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "test-request")
    # Forensic capture writer must not touch disk during these tests.
    monkeypatch.setattr(main, "_capture_queue", None)


def _openai_request(body):
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        receive,
    )


def _anthropic_request(body):
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        receive,
    )


def _gemini_request(body):
    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1internal:streamGenerateContent",
            "query_string": b"alt=sse",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        receive,
    )


async def _drain(response):
    """Fully drain a StreamingResponse body iterator so the egress generator's
    finally block (where obs.log_request fires) actually runs."""
    iterator = response.body_iterator
    try:
        async for _frame in iterator:
            pass
    except StopAsyncIteration:
        pass


def _find_end_log(calls):
    """Pick the END log_request call carrying in_tokens/out_tokens (the stream
    finally site). Probe/intermediate logs may carry other kwargs; the END site
    is the one with both token keys present."""
    for c in reversed(calls):
        if "in_tokens" in c and "out_tokens" in c:
            return c
    return calls[-1] if calls else {}


# ===========================================================================
# Tests 1 & 2 -- unit-level detector split (the (a) fix in quality.py)
# ===========================================================================


def test_detector_accumulates_when_enabled_false_accumulate_true_openai():
    """Spec test 1: with the splice gate OFF (enabled=False) but accumulation ON
    (accumulate=True), feeding OpenAI content chunks MUST accumulate
    partial_text, while `truncated` stays falsy and no splice state is set."""
    det = StreamTruncationDetector("fake/fake-model", enabled=False, accumulate=True)
    for ln in [
        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"world"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
        'data: [DONE]\n\n',
    ]:
        det.feed(ln.encode("utf-8"))
    assert det.partial_text == "Hello world"
    assert not det.truncated


def test_detector_accumulates_when_enabled_false_accumulate_true_anthropic():
    """Spec test 1 (Anthropic dialect): content_block_delta text + thinking
    accumulate under accumulate=True even with enabled=False; truncated stays
    falsy (no message_delta max_tokens splice fires)."""
    det = StreamTruncationDetector("fake/fake-model", enabled=False, accumulate=True)
    for ln in [
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"part1"}}\n\n',
        'data: {"type":"thinking_delta","delta":{"thinking":"think"}}\n\n',
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n',
        'data: [DONE]\n\n',
    ]:
        det.feed(ln.encode("utf-8"))
    assert "part1" in det.partial_text
    assert "think" in det.partial_text
    assert not det.truncated


def test_detector_accumulates_when_enabled_false_accumulate_true_gemini():
    """Spec test 1 (Gemini dialect): candidates parts text accumulates under
    accumulate=True even with enabled=False; a MAX_TOKENS finish does NOT set
    truncated (splice gate off)."""
    det = StreamTruncationDetector("fake/fake-model", enabled=False, accumulate=True)
    for ln in [
        'data: {"candidates":[{"content":{"parts":[{"text":"x"}]}}]}\n\n',
        'data: {"candidates":[{"content":{"parts":[{"text":"y"}]},"finishReason":"MAX_TOKENS"}]}\n\n',
        'data: [DONE]\n\n',
    ]:
        det.feed(ln.encode("utf-8"))
    assert det.partial_text == "xy"
    assert not det.truncated


def test_detector_qwen_path_unchanged_enabled_true_still_truncates():
    """Spec test 2: the enabled=True (Qwen anti-stop) path is bit-identical to
    before -- a length finish still sets truncated, and partial_text still
    accumulates the content. accumulate defaults to inherit-enabled here."""
    det = StreamTruncationDetector("hcnsec-vip/qwen3.8-max", enabled=True)
    for ln in [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
        'data: [DONE]\n\n',
    ]:
        det.feed(ln.encode("utf-8"))
    assert det.truncated
    assert det.partial_text == "Hello world"


def test_detector_disabled_construction_stays_noop_backcompat():
    """Back-compat for the historical `enabled=False` (accumulate unset)
    construction: it inherits accumulate=False, so partial_text stays "" (the
    pre-split behavior that test_stream_anti_stop.test_disabled_detector_is_noop
    pins). The split must NOT change this default."""
    det = StreamTruncationDetector(enabled=False)
    det.feed(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}\n\n')
    assert det.partial_text == ""
    assert not det.truncated


def test_detector_partial_text_cap_bounded():
    """Spec (e): partial_text is capped at PARTIAL_TEXT_CAP_BYTES so a runaway
    stream cannot balloon memory, while partial_len keeps counting every char
    so the len//4 usage estimate stays representative past the cap."""
    det = StreamTruncationDetector("fake/fake-model", enabled=False, accumulate=True)
    cap = StreamTruncationDetector.PARTIAL_TEXT_CAP_BYTES
    big = "a" * (cap + 1000)
    det.feed(('data: {"choices":[{"delta":{"content":' + json.dumps(big) + '}}]}\n\n').encode("utf-8"))
    assert len(det.partial_text) <= cap
    assert det.partial_len >= len(big)


# ===========================================================================
# Tests 3-5 -- CALL-PATH: drive the real egress generators
# ===========================================================================


def test_gemini_lane_backfills_usage_for_non_qwen_provider(monkeypatch):
    """Spec test 3 (THE user regression): client=gemini, provider=mintrouter-
    style (is_qwen=False). Upstream streams OpenAI content but drops the usage
    frame. Driving the REAL convert_gemini_egress path via antigravity_generate,
    the post-stream stats MUST be backfilled: in_tokens>0 AND out_tokens>0.
    Before the fix, partial_text was "" -> backfill early-returned -> 0/0."""
    content = (
        "Quantum tunneling is a phenomenon where a particle passes through a "
        "potential barrier it classically should not. It arises from the wave "
        "nature of matter and underlies stellar fusion and scanning microscopy."
    )
    client = _FakeStreamingClient(_openai_content_chunks([content]))
    logger = _CaptureLogger()
    _block_forensics(monkeypatch, logger)
    # The Gemini (Antigravity IDE) route resolves the model via the integration
    # mapping table, exactly like test_gemini_progressive_stream. Map the IDE
    # model key to the slash-form provider/model so it routes to BSL (not native).
    cfg = _config_openai_provider()
    cfg["antigravity_integration"] = {"enabled": True, "mappings": {"fake-model": "fake/fake-model"}}
    cs.replace_config(cfg)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.antigravity_generate(_gemini_request({
            "project": "test-project",
            "model": "fake-model",
            "userAgent": "antigravity",
            "request": {"contents": [{"role": "user", "parts": [{"text": "Explain tunneling."}]}]},
        }))
        await _drain(response)

    asyncio.run(scenario())
    end = _find_end_log(logger.calls)
    assert end.get("out_tokens", 0) > 0, f"out must be backfilled, got {end}"
    assert end.get("in_tokens", 0) > 0, f"in must be backfilled, got {end}"


def test_openai_pump_lane_backfills_usage_for_non_qwen_provider(monkeypatch):
    """Spec test 4: OpenAI client (/v1/chat/completions) hitting a non-Qwen
    OpenAI-format provider flows through raw_upstream (the _pump lane). Upstream
    streams content but drops the usage frame. The REAL _pump feed+backfill path
    MUST populate stats so in_tokens>0 AND out_tokens>0."""
    content = (
        "Photosynthesis converts light energy into chemical energy stored in "
        "glucose, releasing oxygen as a byproduct of the light-dependent reactions."
    )
    client = _FakeStreamingClient(_openai_content_chunks([content]))
    logger = _CaptureLogger()
    _block_forensics(monkeypatch, logger)
    cs.replace_config(_config_openai_provider())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.chat_completions(_openai_request({
            "model": "fake/fake-model",
            "stream": True,
            "messages": [{"role": "user", "content": "Explain photosynthesis."}],
        }))
        await _drain(response)

    asyncio.run(scenario())
    end = _find_end_log(logger.calls)
    assert end.get("out_tokens", 0) > 0, f"out must be backfilled, got {end}"
    assert end.get("in_tokens", 0) > 0, f"in must be backfilled, got {end}"


def test_anthropic_egress_lane_backfills_usage(monkeypatch):
    """Spec test 5 (convert_egress lane): Anthropic client (/v1/messages) with
    an OpenAI-format upstream. Upstream streams content but drops the usage
    frame. The convert_egress generator (which had feed but NO backfill before
    this fix) MUST now populate stats so in_tokens>0 AND out_tokens>0."""
    content = (
        "The mitochondrion is the powerhouse of the cell: it oxidizes pyruvate "
        "via the Krebs cycle and the electron transport chain to synthesize ATP."
    )
    client = _FakeStreamingClient(_openai_content_chunks([content]))
    logger = _CaptureLogger()
    _block_forensics(monkeypatch, logger)
    cs.replace_config(_config_openai_provider())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.anthropic_messages(_anthropic_request({
            "model": "fake/fake-model",
            "stream": True,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Explain mitochondria."}],
        }))
        await _drain(response)

    asyncio.run(scenario())
    end = _find_end_log(logger.calls)
    assert end.get("out_tokens", 0) > 0, f"out must be backfilled, got {end}"
    assert end.get("in_tokens", 0) > 0, f"in must be backfilled, got {end}"


def test_anthropic_to_openai_egress_lane_backfills_usage(monkeypatch):
    """Spec test 5 (convert_anthropic_to_openai lane): OpenAI client with an
    Anthropic-format upstream (GLM/Kimi/MiniMax style). Upstream streams
    Anthropic content_block_delta frames but drops the usage object. This lane
    had NEITHER feed NOR backfill before the fix; it MUST now populate stats so
    in_tokens>0 AND out_tokens>0."""
    content = (
        "DNA replication is semiconservative: each parental strand serves as a "
        "template for a new complementary strand synthesized by DNA polymerase."
    )
    client = _FakeStreamingClient(_anthropic_content_chunks([content]))
    logger = _CaptureLogger()
    _block_forensics(monkeypatch, logger)
    cs.replace_config(_config_anthropic_provider())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.chat_completions(_openai_request({
            "model": "fakeanthro/fake-model",
            "stream": True,
            "messages": [{"role": "user", "content": "Explain DNA replication."}],
        }))
        await _drain(response)

    asyncio.run(scenario())
    end = _find_end_log(logger.calls)
    assert end.get("out_tokens", 0) > 0, f"out must be backfilled, got {end}"
    assert end.get("in_tokens", 0) > 0, f"in must be backfilled, got {end}"


# ===========================================================================
# Test 6 -- negative: a true dead leaf must NOT be masked
# ===========================================================================


def test_dead_leaf_zero_content_is_not_masked(monkeypatch):
    """Spec test 6: upstream delivers ZERO renderable content (a true dead leaf
    -- only a bare stop frame, no content delta). The detector's partial_text
    stays "", so the backfill early-returns (empty-delivery guard) and out
    stays 0. The [ZeroOutput] / zero-token fallback path is unaffected."""
    # Only the terminal stop frame + [DONE]; no content delta at all.
    dead_leaf_chunks = [
        b'data: {"id":"c","object":"chat.completion.chunk","model":"fake-model",'
        b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    client = _FakeStreamingClient(dead_leaf_chunks)
    logger = _CaptureLogger()
    _block_forensics(monkeypatch, logger)
    cs.replace_config(_config_openai_provider())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda _proxy: client)

    async def scenario():
        response = await main.chat_completions(_openai_request({
            "model": "fake/fake-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }))
        await _drain(response)

    asyncio.run(scenario())
    end = _find_end_log(logger.calls)
    assert end.get("out_tokens", 0) == 0, f"dead leaf must NOT be masked, got {end}"
    assert "usage_estimated" not in end
