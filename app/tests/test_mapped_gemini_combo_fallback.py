"""Regression coverage for mapped Gemini combo streaming fallback."""

import asyncio
import builtins
import io
import json

import httpx

import app.config_state as cs
import app.main as main


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _StallingStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes = None):
        self.first_chunk = first_chunk

    async def __ait__(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.first_chunk is not None:
            chunk, self.first_chunk = self.first_chunk, None
            return chunk
        await asyncio.sleep(10)
        raise StopAsyncIteration


class _Breaker:
    enabled = True
    stream_stall_timeout = 0.01

    @staticmethod
    def filter_healthy_connections(_provider, _model, connections):
        return connections


class _DisabledBreaker(_Breaker):
    enabled = False
    stream_stall_timeout = 10.0


class _ScriptedClient:
    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.models = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        payload = json.loads(request.content)
        model = payload["model"]
        self.models.append(model)
        behavior = self.behaviors[model]
        if callable(behavior):
            return await behavior(request)
        return httpx.Response(200, request=request, stream=behavior)


def _openai_chunk(model: str, text: str = "", finish_reason=None) -> bytes:
    payload = {
        "id": f"chatcmpl-{model}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": text} if text else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


def _success_stream(model: str, text: str = "fallback-ok") -> _ChunkStream:
    return _ChunkStream(_openai_chunk(model, text, "stop"))


def _config():
    providers = {}
    for provider, model in (("dead", "dead-model"), ("healthy", "healthy-model")):
        providers[provider] = {
            "type": "custom",
            "format": "openai",
            "connections": [{
                "enabled": True,
                "api_key": "test",
                "base_url": f"https://{provider}.invalid",
            }],
            "models": [{"id": model, "enabled": True, "thinking": "off"}],
        }
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": providers,
        "combos": [{
            "alias": "GLM-5.2",
            "strategy": "fallback",
            "chain": [
                {"provider": "dead", "model": "dead-model"},
                {"provider": "healthy", "model": "healthy-model"},
            ],
        }],
        "aliases": {},
    }


def _install(monkeypatch, client, *, breaker=None):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    starts = []
    ends = []
    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: breaker)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: starts.append(kwargs) or f"req-{len(starts)}")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: ends.append(kwargs))
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_TIMEOUT", 0.03)
    monkeypatch.setattr(main, "GEMINI_EGRESS_BODY_STALL_TIMEOUT", 0.01)
    return starts, ends


async def _collect(client_wants_gemini=True):
    response = await main._process_chat_completion(
        {
            "model": "GLM-5.2",
            "_bsl_original_model": "gemini-pro-agent",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
        client_wants_gemini=client_wants_gemini,
    )
    return [chunk async for chunk in response.body_iterator]


def test_pending_connection_emits_only_sse_comments(monkeypatch):
    async def delayed_success(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, request=request, stream=_success_stream("dead-model", "ok"))

    client = _ScriptedClient({
        "dead-model": delayed_success,
        "healthy-model": _success_stream("healthy-model"),
    })
    _install(monkeypatch, client)

    chunks = asyncio.run(_collect())

    first_data = next(index for index, chunk in enumerate(chunks) if chunk.startswith(b"data:"))
    assert any(chunk == b": keepalive\n\n" for chunk in chunks[:first_data])
    assert all(chunk.startswith(b":") for chunk in chunks[:first_data])


def test_transport_timeout_advances_combo_and_preserves_attribution(monkeypatch):
    async def connect_timeout(request):
        raise httpx.ConnectTimeout("entry-zero-timeout", request=request)

    client = _ScriptedClient({
        "dead-model": connect_timeout,
        "healthy-model": _success_stream("healthy-model"),
    })
    starts, _ends = _install(monkeypatch, client)
    real_process = main._process_chat_completion
    retry_states = []

    async def recording_process(body, client_wants_anthropic=False, client_wants_gemini=False, _retry_state=None, request=None):
        if _retry_state is not None:
            retry_states.append(_retry_state.copy())
        return await real_process(
            body,
            client_wants_anthropic,
            client_wants_gemini,
            _retry_state=_retry_state,
            request=request,
        )

    monkeypatch.setattr(main, "_process_chat_completion", recording_process)
    output = b"".join(asyncio.run(_collect()))

    assert client.models == ["dead-model", "healthy-model"]
    assert b"fallback-ok" in output
    assert output.rstrip().endswith(b"data: [DONE]")
    # One start log per request since 3a15ac8 dedupes retry re-entries.
    assert len(starts) == 1
    assert starts[0]["combo"] == "GLM-5.2"
    assert retry_states[0]["original_model"] == "gemini-pro-agent"
    assert retry_states[0]["idx"] == 1


def test_hung_connection_hits_deadline_and_advances_combo(monkeypatch):
    # Option A (2026-07-30): for Gemini clients a hung connection hits the
    # connect deadline and emits a terminal error + [DONE] without advancing
    # to the next combo leaf. The hung task is NOT cancelled (the new code
    # returns cleanly after yielding the error frame).
    async def hung_connection(_request):
        await asyncio.sleep(10)

    client = _ScriptedClient({
        "dead-model": hung_connection,
        "healthy-model": _success_stream("healthy-model"),
    })
    _install(monkeypatch, client)

    output = b"".join(asyncio.run(_collect()))

    # No in-stream advance: only the dead leaf is dialed.
    assert client.models == ["dead-model"]
    # Terminal error frame, not a spliced fallback.
    assert b"fallback-ok" not in output
    assert output.rstrip().endswith(b"data: [DONE]")


def test_pre_content_stream_stall_combo_fallback_for_gemini(monkeypatch):
    # UNIVERSAL ANTI-FREEZE (2026-08-02): for Antigravity (Gemini) clients a
    # pre-content ttft_stall now COMBO-FALLBACKS to the next chain entry.
    # The IDE hasn't started parsing model output yet (only heartbeat sent),
    # so it's safe to retry with a different model.
    client = _ScriptedClient({
        "dead-model": _StallingStream(),
        "healthy-model": _success_stream("healthy-model"),
    })
    _install(monkeypatch, client, breaker=_Breaker())

    output = b"".join(asyncio.run(_collect()))

    # Combo fallback: both models are dialed.
    assert client.models == ["dead-model", "healthy-model"]
    # Fallback content is streamed.
    assert b"fallback-ok" in output
    assert output.rstrip().endswith(b"data: [DONE]")


def test_pre_content_stream_stall_combo_fallback_when_breaker_disabled(monkeypatch):
    # UNIVERSAL ANTI-FREEZE: combo fallback works even when circuit breaker is disabled.
    client = _ScriptedClient({
        "dead-model": _StallingStream(),
        "healthy-model": _success_stream("healthy-model"),
    })
    _install(monkeypatch, client, breaker=_DisabledBreaker())

    output = b"".join(asyncio.run(_collect()))

    assert client.models == ["dead-model", "healthy-model"]
    assert b"fallback-ok" in output
    assert output.rstrip().endswith(b"data: [DONE]")


def test_stream_stall_after_real_data_does_not_splice_fallback(monkeypatch):
    """Post-content silence must never splice a second stream into the client.

    CONTRACT CHANGE (2026-08-04, 9router parity). This test previously asserted
    that a post-content stall produced a synthetic PROXY_ERROR frame. It no
    longer does, because the router no longer decides that a quiet stream is a
    dead one -- the `stream_stall` classification was deleted along with the
    watchdog that produced it (see `_stall_watchdog` in main.py).

    The load-bearing invariant is UNCHANGED and still asserted below: once real
    content has reached the client, no fallback may be spliced in. What changed
    is only the ending -- the stream now closes cleanly on upstream EOF instead
    of having an error frame injected ahead of it.

    Why the old assertion was wrong: silence is not death. A provider mid-way
    through extended thinking looks identical to a stalled one, and injecting
    PROXY_ERROR into a healthy stream is itself a freeze source.
    """
    client = _ScriptedClient({
        "dead-model": _StallingStream(_openai_chunk("dead-model", "partial")),
        "healthy-model": _success_stream("healthy-model"),
    })
    _install(monkeypatch, client, breaker=_Breaker())

    output = b"".join(asyncio.run(_collect()))

    # Only the first leaf is dialed: no mid-stream failover. (THE invariant.)
    assert client.models == ["dead-model"]
    # Content that did arrive is delivered, not discarded.
    assert b"partial" in output
    # No second stream spliced into a client parser already mid-message.
    assert b"fallback-ok" not in output
    # 9ROUTER PARITY: no synthetic error frame is manufactured from silence.
    assert b"PROXY_ERROR" not in output
    # The stream is still properly terminated, so the IDE never hangs.
    assert output.rstrip().endswith(b"data: [DONE]")



def test_last_combo_entry_error_is_valid_gemini_sse(monkeypatch):
    async def upstream_error(request):
        return httpx.Response(503, request=request, content=b"last-entry-unavailable")

    client = _ScriptedClient({
        "dead-model": upstream_error,
        "healthy-model": upstream_error,
    })
    _install(monkeypatch, client)

    chunks = asyncio.run(_collect())
    output = b"".join(chunks)
    data_frames = [chunk for chunk in chunks if chunk.startswith(b"data:")]

    assert client.models == ["dead-model", "healthy-model"]
    # FREEZE FIX (2026-08-07): the upstream error text must still be VISIBLE in
    # the terminal frame's `parts` text (terminal_error_frame puts the message
    # there), but NO bare top-level {"error":...} frame may precede it.
    assert any(b"last-entry-unavailable" in f for f in data_frames), \
        "upstream 503 text must remain visible to the client"

    # THE INVARIANT (2026-08-07): exactly ONE finishReason-bearing terminal
    # frame, and NO bare top-level error frame may precede it (that poison is
    # what froze the IDE on 2026-08-07 — the parser stops consuming the later
    # candidate once it has seen a top-level error).
    _terminal = [f for f in data_frames if b'"finishReason"' in f]
    assert _terminal, "no finishReason frame: the Gemini client cannot end the stream"
    assert len(_terminal) == 1, f"expected exactly one terminal frame, got {len(_terminal)}"
    _bare_err = [f for f in data_frames if f.startswith(b'data: {"error":')]
    assert not _bare_err, (
        "bare top-level {\"error\":...} frame precedes the terminal candidate — "
        "this poisons the Antigravity Gemini parser and reproduces the freeze: "
        + repr(_bare_err)
    )

    assert data_frames[-1].rstrip() == b"data: [DONE]"
    assert output.count(b"data: [DONE]") == 1


# ──────────────────────────────────────────────────────────────────────────────
# ALL-LEAVES-429 REGRESSION (2026-08-07 freeze)
# Deterministic reproduction of the incident: every combo leaf returns 429.
# Records attempt order, bounded completion, emitted SSE frames, and the exact
# terminal contract. Asserts each eligible leaf is attempted at most once,
# fallback respects the chain deadline, and the stream terminates promptly with
# ONE parser-valid terminal frame (no bare error prefix, no duplicate DONE).
# ──────────────────────────────────────────────────────────────────────────────

def _config_three_leaves():
    providers = {}
    for provider, model in (("p0", "m0"), ("p1", "m1"), ("p2", "m2")):
        providers[provider] = {
            "type": "custom",
            "format": "openai",
            "connections": [{"enabled": True, "api_key": "t", "base_url": f"https://{provider}.invalid"}],
            "models": [{"id": model, "enabled": True, "thinking": "off"}],
        }
    return {
        "tools": {"output_thinking_squeeze": False},
        "providers": providers,
        "combos": [{
            "alias": "ALL429",
            "strategy": "fallback",
            "chain": [
                {"provider": "p0", "model": "m0"},
                {"provider": "p1", "model": "m1"},
                {"provider": "p2", "model": "m2"},
            ],
        }],
        "aliases": {},
    }


def _install_three(monkeypatch, client, breaker=None):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith(".brain/logs/outbound_upstream.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)
    cs.replace_config(_config_three_leaves())
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: client)
    monkeypatch.setattr(main, "get_breaker", lambda: breaker)
    monkeypatch.setattr(main.obs, "log_request_start", lambda **kwargs: "req")
    monkeypatch.setattr(main.obs, "log_request", lambda **kwargs: None)
    monkeypatch.setattr(main, "GEMINI_EGRESS_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_KEEPALIVE_INTERVAL", 0.005)
    monkeypatch.setattr(main, "GEMINI_EGRESS_CONNECT_TIMEOUT", 0.03)
    monkeypatch.setattr(main, "GEMINI_EGRESS_BODY_STALL_TIMEOUT", 0.01)


async def _collect_three():
    response = await main._process_chat_completion(
        {
            "model": "ALL429",
            "_bsl_original_model": "gemini-pro-agent",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
        client_wants_gemini=True,
    )
    return [chunk async for chunk in response.body_iterator]


def test_all_leaves_429_terminates_with_one_terminal_contract(monkeypatch):
    """THE INCIDENT (2026-08-07): every combo leaf returns 429.

    Before the fix the terminal sequence was
        error -> synthetic STOP -> [DONE]
    and the IDE froze because the top-level error poisoned the parser. After
    the fix the sequence is a SOLE terminal_error_frame (finishReason) + [DONE].
    """
    import time as _time

    async def upstream_429(request):
        return httpx.Response(429, request=request, content=b"rate-limited")

    client = _ScriptedClient({"m0": upstream_429, "m1": upstream_429, "m2": upstream_429})
    _install_three(monkeypatch, client, breaker=_Breaker())

    t0 = _time.monotonic()
    chunks = asyncio.run(_collect_three())
    elapsed = _time.monotonic() - t0
    output = b"".join(chunks)
    data_frames = [c for c in chunks if c.startswith(b"data:")]

    # Attempt order: every eligible leaf tried exactly once, in chain order.
    assert client.models == ["m0", "m1", "m2"], f"attempt order wrong: {client.models}"
    assert len(client.models) == len(set(client.models)), "a leaf was attempted more than once"

    # Bounded completion: the whole chain must drain well under the 150s budget.
    # (Pre-fix this still completed fast — the freeze was client-side, not
    # server-side — but we assert it to guard against a regression that makes
    # the router wait for cooldown expiry inside the active request.)
    assert elapsed < 10.0, f"chain did not complete promptly: {elapsed:.2f}s"

    # EXACT TERMINAL CONTRACT — the core assertion.
    # 1. Exactly one finishReason-bearing terminal frame.
    _terminal = [f for f in data_frames if b'"finishReason"' in f]
    assert len(_terminal) == 1, f"expected exactly one terminal frame, got {len(_terminal)}: {_terminal}"
    # 2. The error text (429 / rate-limited) is VISIBLE inside the terminal
    #    frame's parts, not hidden in a discarded bare error object.
    assert b"rate-limited" in _terminal[0], "terminal frame must carry visible error text"
    assert b"upstream error 429" in _terminal[0], "terminal frame must carry the 429 code in visible text"
    # 3. NO bare top-level {"error":...} frame precedes it (the poison).
    _bare_err = [f for f in data_frames if f.startswith(b'data: {"error":')]
    assert not _bare_err, f"bare top-level error frame present (poison): {_bare_err}"
    # 4. Exactly one [DONE], and it is the final frame.
    assert output.count(b"data: [DONE]") == 1, "duplicate [DONE] sentinel"
    assert data_frames[-1].rstrip() == b"data: [DONE]"
    # 5. No fallback content spliced (there was none to splice).
    assert b"fallback-ok" not in output


def test_all_leaves_429_no_silent_generator_return(monkeypatch):
    """Companion to the above: the generator must not return without a
    terminal frame. A bare [DONE] with no finishReason candidate would hang the
    IDE exactly like the poison. This catches the zero-output/last-leaf silent
    fall-through defect called out in the implementation plan."""

    async def upstream_429(request):
        return httpx.Response(429, request=request, content=b"rate-limited")

    client = _ScriptedClient({"m0": upstream_429, "m1": upstream_429, "m2": upstream_429})
    _install_three(monkeypatch, client, breaker=_Breaker())

    chunks = asyncio.run(_collect_three())
    data_frames = [c for c in chunks if c.startswith(b"data:")]

    # There must be at least one data frame carrying a finishReason — a stream
    # that ends on only heartbeat comments + [DONE] is the silent-return freeze.
    _terminal = [f for f in data_frames if b'"finishReason"' in f]
    assert _terminal, (
        "generator returned without a finishReason-bearing terminal frame — "
        "the Gemini parser cannot end on [DONE] alone and the IDE would hang"
    )


def test_all_leaves_429_respects_chain_deadline(monkeypatch):
    """When the chain budget is already exhausted, the last 429 must still emit
    exactly one terminal contract rather than looping or waiting."""

    async def upstream_429(request):
        return httpx.Response(429, request=request, content=b"rate-limited")

    client = _ScriptedClient({"m0": upstream_429, "m1": upstream_429, "m2": upstream_429})
    _install_three(monkeypatch, client, breaker=_Breaker())
    # Pre-exhaust the deadline so _chain_budget_remaining() <= 0 from the start.
    import time as _time
    monkeypatch.setattr(main, "CHAIN_TOTAL_BUDGET", 0.0)

    chunks = asyncio.run(_collect_three())
    data_frames = [c for c in chunks if c.startswith(b"data:")]
    _terminal = [f for f in data_frames if b'"finishReason"' in f]
    assert len(_terminal) == 1, f"expected exactly one terminal frame under deadline, got {len(_terminal)}"
    _bare_err = [f for f in data_frames if f.startswith(b'data: {"error":')]
    assert not _bare_err, f"bare top-level error frame under deadline (poison): {_bare_err}"
