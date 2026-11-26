"""Lifecycle, phase-separation, non-replay, and resource cleanup contracts for Studio native transport."""
import asyncio
import importlib.util
import json
import pathlib
import re

import httpx
import pytest


def _bridge():
    path = (
        pathlib.Path(__file__).parents[2]
        / ".."
        / "Studio Setup"
        / "packages"
        / "skills-hub"
        / "hooks"
        / "bsl-studio-runtime-bridge.py"
    )
    if not path.is_file():
        pytest.skip("Optional Studio runtime bridge checkout is not installed")
    spec = importlib.util.spec_from_file_location("bsl_studio_bridge_test", path.resolve())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_terminal_sse_code(chunks: list[bytes]) -> tuple[int, str]:
    joined = b"".join(chunks).decode("utf-8", errors="replace")
    for line in joined.splitlines():
        if line.startswith("data: ") and "[DONE]" not in line:
            payload = json.loads(line[len("data: ") :])
            err = payload.get("error") or {}
            if err:
                return int(err.get("code", 0)), str(err.get("message", ""))
            candidates = (payload.get("response") or {}).get("candidates") or []
            if candidates:
                parts = ((candidates[0].get("content") or {}).get("parts") or [])
                if parts:
                    text = str(parts[0].get("text") or "")
                    m = re.search(r"\[BSL Router\] upstream error (\d+):\s*(.*)", text, re.S)
                    if m:
                        return int(m.group(1)), m.group(2)
    raise AssertionError(f"No terminal JSON error frame found in SSE chunks: {joined!r}")


def test_dispose_send_closes_response_completed_before_cancel():
    bridge = _bridge()

    class Response:
        def __init__(self):
            self.closed = 0

        async def aclose(self):
            self.closed += 1

    async def scenario():
        response = Response()

        async def send():
            return response

        task = asyncio.create_task(send())
        await asyncio.sleep(0)
        await bridge._studio_dispose_send(task)
        assert response.closed == 1

    asyncio.run(scenario())


def test_native_open_respects_injected_and_monkeypatched_client_ownership():
    bridge = _bridge()

    class SharedClient:
        is_closed = False

        def build_request(self, *args, **kwargs):
            class Req:
                extensions = {}

            return Req()

        async def send(self, request, stream=True):
            return object()

        async def aclose(self):
            raise AssertionError("shared/injected client must stay open")

    orig_getter = lambda: SharedClient()

    class ModInjected:
        google_egress_client = SharedClient()
        _studio_orig_antigravity_egress_getter = orig_getter
        _get_antigravity_egress_client = staticmethod(orig_getter)
        build_google_egress_client = staticmethod(lambda: AssertionError("factory should not be called"))
        _build_antigravity_native_url = staticmethod(lambda request: "https://example.invalid")
        _antigravity_native_request_headers = staticmethod(lambda request: {})

    class ModMonkeypatched:
        google_egress_client = None
        _studio_orig_antigravity_egress_getter = orig_getter
        _get_antigravity_egress_client = staticmethod(lambda: SharedClient())
        build_google_egress_client = staticmethod(lambda: AssertionError("factory should not override monkeypatch"))
        _build_antigravity_native_url = staticmethod(lambda request: "https://example.invalid")
        _antigravity_native_request_headers = staticmethod(lambda request: {})

    async def scenario():
        for mod_cls in (ModInjected, ModMonkeypatched):
            state = {}
            opener = bridge._studio_native_open(mod_cls, object(), b"{}", state)
            try:
                await opener.__anext__()
            except StopAsyncIteration:
                pass
            await opener.aclose()
            assert state["owned"] is False
            assert state["response"] is not None

    asyncio.run(scenario())


def test_phase_transition_extends_post_upload_deadline_and_emits_keepalives(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_CONNECT_WRITE_S", 0.08)
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_RESPONSE_HEADERS_S", 0.35)
    monkeypatch.setattr(bridge, "_SHIELD16_KEEPALIVE_INTERVAL_S", 0.025)

    class OkResponse:
        status_code = 200

        async def aclose(self):
            return None

    class OwnedClient:
        def __init__(self):
            self.closed = 0

        def build_request(self, *args, **kwargs):
            class Req:
                def __init__(self):
                    self.extensions = {}

            return Req()

        async def send(self, req, stream=True):
            await asyncio.sleep(0.02)
            trace = req.extensions.get("trace")
            assert callable(trace)
            await trace("http11.send_request_body.complete", {"secret": "Bearer MUST_NOT_LOG"})
            # Total elapsed ~0.16s > connect/upload 0.08s ceiling, proving phase transition switched to 0.35s ceiling
            await asyncio.sleep(0.14)
            return OkResponse()

        async def aclose(self):
            self.closed += 1

    created = []

    class Mod:
        google_egress_client = None
        _studio_orig_antigravity_egress_getter = None

        @staticmethod
        def build_google_egress_client():
            c = OwnedClient()
            created.append(c)
            return c

        _build_antigravity_native_url = staticmethod(lambda request: "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
        _antigravity_native_request_headers = staticmethod(lambda request: {})

    async def scenario():
        state = {}
        keepalives = []
        opener = bridge._studio_native_open(Mod, object(), b'{"model":"models/gemini-3.1-pro-high"}', state)
        try:
            async for k in opener:
                keepalives.append(k)
        finally:
            await opener.aclose()
        assert len(keepalives) >= 2
        assert state["write_complete"] is True
        assert state["phase"] == "response_headers"
        assert state["owned"] is True
        assert state["response"].status_code == 200
        assert created[0].closed == 0

    asyncio.run(scenario())


def test_post_upload_header_timeout_emits_504_without_replay(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_CONNECT_WRITE_S", 0.25)
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_RESPONSE_HEADERS_S", 0.07)
    monkeypatch.setattr(bridge, "_SHIELD16_KEEPALIVE_INTERVAL_S", 0.02)

    counters = {"send": 0, "aclose": 0}

    class HangingPostWriteClient:
        def build_request(self, *args, **kwargs):
            class Req:
                def __init__(self):
                    self.extensions = {}

            return Req()

        async def send(self, req, stream=True):
            counters["send"] += 1
            trace = req.extensions["trace"]
            await trace("http11.send_request_body.complete", {})
            await asyncio.sleep(5.0)
            return object()

        async def aclose(self):
            counters["aclose"] += 1

    class Mod:
        google_egress_client = None
        _studio_orig_antigravity_egress_getter = None
        GEMINI_SSE_DONE = b"data: [DONE]\r\n\r\n"
        build_google_egress_client = staticmethod(lambda: HangingPostWriteClient())
        _build_antigravity_native_url = staticmethod(lambda request: "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
        _antigravity_native_request_headers = staticmethod(lambda request: {})
        gemini_sse_data = staticmethod(lambda obj: f"data: {json.dumps(obj)}\r\n\r\n".encode("utf-8"))

    async def scenario():
        chunks = []
        body = b'{"model":"models/gemini-3.1-pro-high","request":{"contents":[]}}'
        async for chunk in bridge._studio_keepalive_opening_and_resilient_stream(Mod, object(), body):
            chunks.append(chunk)
        # Critical invariant: NEVER replay acknowledged prefill on post-upload header timeout
        assert counters["send"] == 1
        assert counters["aclose"] == 1
        code, msg = _parse_terminal_sse_code(chunks)
        assert code == 504
        assert "response_headers" in msg

    asyncio.run(scenario())


def test_pre_upload_connect_error_retries_4x_closes_owned_clients_and_emits_502(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_CONNECT_WRITE_S", 0.2)
    monkeypatch.setattr(bridge, "_SHIELD16_KEEPALIVE_INTERVAL_S", 0.02)

    orig_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await orig_sleep(min(delay, 0.005))

    monkeypatch.setattr(bridge.asyncio, "sleep", fast_sleep)

    counters = {"send": 0, "aclose": 0, "stage16b": 0}

    async def forbidden_stage16b(*args, **kwargs):
        counters["stage16b"] += 1
        yield b"should-never-run"

    monkeypatch.setattr(bridge, "_stream_stage16b_live_failover_sse", forbidden_stage16b)

    class ConnectFailClient:
        def build_request(self, *args, **kwargs):
            class Req:
                def __init__(self):
                    self.extensions = {}

            return Req()

        async def send(self, req, stream=True):
            counters["send"] += 1
            raise httpx.ConnectError("edge ip unreachable")

        async def aclose(self):
            counters["aclose"] += 1

    # Deliberately omit rotate_google_ip_cache on Mod to prove _studio_rotate_google_ip_cache guard works
    class Mod:
        google_egress_client = None
        _studio_orig_antigravity_egress_getter = None
        GEMINI_SSE_DONE = b"data: [DONE]\r\n\r\n"
        build_google_egress_client = staticmethod(lambda: ConnectFailClient())
        _build_antigravity_native_url = staticmethod(lambda request: "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
        _antigravity_native_request_headers = staticmethod(lambda request: {})
        gemini_sse_data = staticmethod(lambda obj: f"data: {json.dumps(obj)}\r\n\r\n".encode("utf-8"))

    async def scenario():
        chunks = []
        body = b'{"model":"models/gemini-3.1-pro-high","request":{"contents":[]}}'
        async for chunk in bridge._studio_keepalive_opening_and_resilient_stream(Mod, object(), body):
            chunks.append(chunk)
        assert counters["send"] == 4
        assert counters["aclose"] == 4
        assert counters["stage16b"] == 0
        code, msg = _parse_terminal_sse_code(chunks)
        assert code == 502
        assert "ConnectError" in msg

    asyncio.run(scenario())


def test_native_error_body_bounds_64kib_and_timeout_closes_response(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_ERROR_BODY_S", 0.06)
    monkeypatch.setattr(bridge, "_SHIELD16_KEEPALIVE_INTERVAL_S", 0.02)

    class OversizedErrorResponse:
        def __init__(self):
            self.closed = 0

        async def aiter_raw(self):
            for _ in range(5):
                yield b"X" * 30000

        async def aclose(self):
            self.closed += 1

    class HangingErrorResponse:
        def __init__(self):
            self.closed = 0

        async def aiter_raw(self):
            yield b"partial"
            await asyncio.sleep(5.0)

        async def aclose(self):
            self.closed += 1

    async def scenario():
        r1 = OversizedErrorResponse()
        s1 = {}
        async for _ in bridge._studio_native_error_body(r1, s1):
            pass
        assert len(s1["body"]) == 65536
        assert r1.closed == 1

        r2 = HangingErrorResponse()
        s2 = {}
        keepalives = []
        async for k in bridge._studio_native_error_body(r2, s2):
            keepalives.append(k)
        assert len(keepalives) >= 1
        assert s2["body"] == b"upstream error body deadline exceeded"
        assert r2.closed == 1

    asyncio.run(scenario())


def test_native_open_enforces_sse_and_identity_encoding():
    bridge = _bridge()
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, text='data: {}\n\n', headers={'content-type': 'text/event-stream'})

    async def scenario():
        from types import SimpleNamespace
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mod = SimpleNamespace(
                google_egress_client=client,
                _build_antigravity_native_url=lambda request: 'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?other=keep',
                _antigravity_native_request_headers=lambda request: {'accept-encoding': 'gzip', 'accept': 'application/json'},
            )
            state = {}
            async for _ in bridge._studio_native_open(mod, object(), b'{"model":"gemini-4-carbon-high"}', state):
                pass
            assert seen[0].url.params['alt'] == 'sse'
            assert seen[0].url.params['other'] == 'keep'
            assert seen[0].headers['accept'] == 'text/event-stream'
            assert seen[0].headers['accept-encoding'] == 'identity'
            await state['response'].aclose()
    asyncio.run(scenario())


def test_auto_retry_shield_skips_deterministic_session_and_arena_errors(monkeypatch):
    from types import SimpleNamespace
    from starlette.responses import JSONResponse, StreamingResponse

    bridge = _bridge()

    session_sse = b'data: {"error":{"message":"Gemini upstream (session_unavailable): gemini.google.com tab on :9222 is signed out (user@example.com)","type":"api_error","code":502}}\n\n'
    arena_exhausted_sse = b'data: {"error":{"message":"arena-free failover exhausted","type":"api_error","code":503}}\n\n'
    arena_invalid_sse = b'data: {"error":{"message":"Arena upstream (invalid_model): UI-drive: model \'gemini-4-argon-high\' not found in the native picker","code":502}}\n\n'
    transient_sse = b'data: {"error":{"message":"Upstream temporarily unavailable","code":503}}\n\n'

    assert bridge._is_pre_token_retryable_error_chunk(session_sse) is False
    assert bridge._is_pre_token_retryable_error_chunk(arena_exhausted_sse) is False
    assert bridge._is_pre_token_retryable_error_chunk(arena_invalid_sse) is False
    assert bridge._is_pre_token_retryable_error_chunk(transient_sse) is True

    async def fast_sleep(_delay):
        return None

    monkeypatch.setattr(bridge.asyncio, "sleep", fast_sleep)

    for _sub_patcher in (
        "patch_google_egress_module",
        "patch_antifreeze_module",
        "patch_antigravity_upstream_module",
        "patch_faithful_execution_module",
        "patch_combo_resolver_module",
        "patch_gemini_adapter_module",
        "patch_reasoning_policy_module",
        "patch_caching_adapter_module",
        "_install_isolated_native_egress_bindings",
    ):
        if hasattr(bridge, _sub_patcher):
            monkeypatch.setattr(bridge, _sub_patcher, lambda *_a, **_kw: None)

    def make_patched_mod(handler):
        mod = SimpleNamespace(
            _merge_antigravity_custom_models_catalog=lambda u, c: u,
            _get_antigravity_egress_client=lambda: None,
            _forward_antigravity_native=lambda *a, **kw: None,
            _process_chat_completion=handler,
            _compare_versions=lambda a, b: 0,
        )
        bridge.patch_app_main_module(mod)
        return mod

    async def scenario():
        # 1) Stream 502 session_unavailable runs strictly ONCE (no 3x Shield outer retries)
        stream_calls = 0

        async def sess_stream_handler(*args, **kwargs):
            nonlocal stream_calls
            stream_calls += 1

            async def _gen():
                yield session_sse
                yield b"data: [DONE]\n\n"

            return StreamingResponse(_gen(), media_type="text/event-stream")

        mod1 = make_patched_mod(sess_stream_handler)
        req1 = SimpleNamespace(model="Gemini-4-Carbon", headers={})
        resp1 = await mod1._process_chat_completion(req1, False, False, None, SimpleNamespace(headers={}))
        chunks1 = [c async for c in resp1.body_iterator]
        assert stream_calls == 1
        assert session_sse in b"".join(chunks1)

        # 2) Non-stream HTTP 502 session_unavailable runs strictly ONCE
        nonstream_calls = 0

        async def sess_nonstream_handler(*args, **kwargs):
            nonlocal nonstream_calls
            nonstream_calls += 1
            return JSONResponse(
                {"error": {"message": "Gemini upstream (session_unavailable): signed out", "code": 502}},
                status_code=502,
            )

        mod2 = make_patched_mod(sess_nonstream_handler)
        req2 = SimpleNamespace(model="Gemini-4-Carbon", headers={})
        resp2 = await mod2._process_chat_completion(req2, False, False, None, SimpleNamespace(headers={}))
        assert resp2.status_code == 502
        assert nonstream_calls == 1

        # 3) Transient 503 stream still auto-retries and recovers cleanly
        trans_calls = 0

        async def transient_handler(*args, **kwargs):
            nonlocal trans_calls
            trans_calls += 1

            async def _gen():
                if trans_calls == 1:
                    yield transient_sse
                    yield b"data: [DONE]\n\n"
                else:
                    yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                    yield b"data: [DONE]\n\n"

            return StreamingResponse(_gen(), media_type="text/event-stream")

        mod3 = make_patched_mod(transient_handler)
        req3 = SimpleNamespace(model="Gemini-4-Carbon", headers={})
        resp3 = await mod3._process_chat_completion(req3, False, False, None, SimpleNamespace(headers={}))
        joined3 = b"".join([c async for c in resp3.body_iterator])
        assert trans_calls == 2
        assert b'"content":"ok"' in joined3

    asyncio.run(scenario())


def test_post_upload_dead_socket_readerror_retries_with_isolated_ip_rotation_and_recovers(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_CONNECT_WRITE_S", 0.5)
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_RESPONSE_HEADERS_S", 1.0)
    monkeypatch.setattr(bridge, "_SHIELD16_KEEPALIVE_INTERVAL_S", 0.02)

    orig_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await orig_sleep(min(delay, 0.005))

    monkeypatch.setattr(bridge.asyncio, "sleep", fast_sleep)

    counters = {"send": 0, "aclose": 0, "native_rotates": 0, "router_rotates": 0}

    class OkStreamResp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_raw(self):
            yield b'data: {"response":{"candidates":[{"content":{"role":"model","parts":[{"text":"recovered"}]},"finishReason":"STOP"}]}}\r\n\r\n'

        async def aclose(self):
            return None

    class FlakyReadErrorClient:
        def build_request(self, *args, **kwargs):
            class Req:
                def __init__(self):
                    self.extensions = {}

            return Req()

        async def send(self, req, stream=True):
            counters["send"] += 1
            trace = req.extensions["trace"]
            await trace("http11.send_request_body.complete", {})
            if counters["send"] == 1:
                raise httpx.ReadError("")
            return OkStreamResp()

        async def aclose(self):
            counters["aclose"] += 1

    class Mod:
        google_egress_client = None
        _studio_orig_antigravity_egress_getter = None
        GEMINI_SSE_DONE = b"data: [DONE]\r\n\r\n"
        build_google_egress_client = staticmethod(lambda: FlakyReadErrorClient())
        _build_antigravity_native_url = staticmethod(lambda request: "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
        _antigravity_native_request_headers = staticmethod(lambda request: {})
        gemini_sse_data = staticmethod(lambda obj: f"data: {json.dumps(obj)}\r\n\r\n".encode("utf-8"))

        @staticmethod
        def _studio_rotate_native_google_ip_cache():
            counters["native_rotates"] += 1

        @staticmethod
        def rotate_google_ip_cache():
            counters["router_rotates"] += 1

    async def scenario():
        chunks = []
        body = b'{"model":"gemini-4-neon-xhigh","request":{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}}'
        async for chunk in bridge._studio_keepalive_opening_and_resilient_stream(Mod, object(), body):
            chunks.append(chunk)
        joined = b"".join(chunks)
        assert counters["send"] == 2
        assert counters["aclose"] == 2
        assert counters["native_rotates"] == 1
        assert counters["router_rotates"] == 0
        assert b"recovered" in joined
        assert b"upstream error 502" not in joined

    asyncio.run(scenario())


def test_post_upload_repeated_readerror_exhausts_3_attempts_with_nonempty_detail(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_CONNECT_WRITE_S", 0.5)
    monkeypatch.setattr(bridge, "_STUDIO_NATIVE_RESPONSE_HEADERS_S", 1.0)
    monkeypatch.setattr(bridge, "_SHIELD16_KEEPALIVE_INTERVAL_S", 0.02)

    orig_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await orig_sleep(min(delay, 0.005))

    monkeypatch.setattr(bridge.asyncio, "sleep", fast_sleep)

    counters = {"send": 0, "aclose": 0}

    class AlwaysResetClient:
        def build_request(self, *args, **kwargs):
            class Req:
                def __init__(self):
                    self.extensions = {}

            return Req()

        async def send(self, req, stream=True):
            counters["send"] += 1
            trace = req.extensions["trace"]
            await trace("http11.send_request_body.complete", {})
            raise httpx.ReadError("")

        async def aclose(self):
            counters["aclose"] += 1

    class Mod:
        google_egress_client = None
        _studio_orig_antigravity_egress_getter = None
        GEMINI_SSE_DONE = b"data: [DONE]\r\n\r\n"
        build_google_egress_client = staticmethod(lambda: AlwaysResetClient())
        _build_antigravity_native_url = staticmethod(lambda request: "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
        _antigravity_native_request_headers = staticmethod(lambda request: {})
        gemini_sse_data = staticmethod(lambda obj: f"data: {json.dumps(obj)}\r\n\r\n".encode("utf-8"))

    async def scenario():
        chunks = []
        body = b'{"model":"gemini-4-neon-xhigh","request":{"contents":[]}}'
        async for chunk in bridge._studio_keepalive_opening_and_resilient_stream(Mod, object(), body):
            chunks.append(chunk)
        assert counters["send"] == 3
        assert counters["aclose"] == 3
        code, msg = _parse_terminal_sse_code(chunks)
        assert code == 502
        assert "ReadError" in msg
        assert not msg.rstrip().endswith(":")

    asyncio.run(scenario())


def test_native_google_egress_dns_cache_isolated_from_router_and_keepalive_enabled():
    import socket
    import app.utils.google_cloudcode_egress as ge

    bridge = _bridge()
    bridge.patch_google_egress_module(ge, force=True)

    assert callable(getattr(ge, "build_native_google_egress_client", None))
    assert callable(getattr(ge, "rotate_native_google_ip_cache", None))
    assert getattr(ge.build_google_egress_client, "_is_studio_google_egress_builder", False) is True

    # Seed both caches and prove router-side invalidation/rotation NEVER touches native cache
    with ge._STUDIO_NATIVE_CACHE_LOCK:
        ge._STUDIO_NATIVE_RESOLVE_CACHE["daily-cloudcode-pa.googleapis.com"] = (
            ["172.217.116.4", "172.217.118.4"],
            9999999999.0,
        )
    with ge._cache_lock:
        ge._RESOLVE_CACHE["daily-cloudcode-pa.googleapis.com"] = (
            ["172.217.112.4", "172.217.113.4"],
            9999999999.0,
        )

    ge.rotate_google_ip_cache("daily-cloudcode-pa.googleapis.com")
    with ge._cache_lock:
        ge._RESOLVE_CACHE.clear()

    with ge._STUDIO_NATIVE_CACHE_LOCK:
        assert ge._STUDIO_NATIVE_RESOLVE_CACHE["daily-cloudcode-pa.googleapis.com"][0] == [
            "172.217.116.4",
            "172.217.118.4",
        ]

    ge.rotate_native_google_ip_cache("daily-cloudcode-pa.googleapis.com")
    with ge._STUDIO_NATIVE_CACHE_LOCK:
        assert ge._STUDIO_NATIVE_RESOLVE_CACHE["daily-cloudcode-pa.googleapis.com"][0] == [
            "172.217.118.4",
            "172.217.116.4",
        ]

    # Verify TCP keepalive helper sets SO_KEEPALIVE + TCP_NODELAY + Windows SIO_KEEPALIVE_VALS
    class FakeSock:
        def __init__(self):
            self.opts = []
            self.ioctls = []

        def setsockopt(self, level, optname, value):
            self.opts.append((level, optname, value))

        def ioctl(self, control, option):
            self.ioctls.append((control, option))

    class FakeStream:
        def __init__(self, sock):
            self._s = sock

        def get_extra_info(self, key):
            return self._s if key == "socket" else None

    fs = FakeSock()
    bridge._enable_studio_tcp_keepalive(FakeStream(fs), idle_ms=10000, interval_ms=5000)
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in fs.opts
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in fs.opts
    if hasattr(socket, "SIO_KEEPALIVE_VALS"):
        assert (socket.SIO_KEEPALIVE_VALS, (1, 10000, 5000)) in fs.ioctls


def test_isolated_native_egress_getter_uses_native_builder_and_honors_monkeypatches():
    import types
    import app.utils.google_cloudcode_egress as ge

    bridge = _bridge()
    bridge.patch_google_egress_module(ge, force=True)

    orig_called = []

    def orig_getter():
        orig_called.append(True)
        return "orig-client"

    fake_mod = types.SimpleNamespace(
        google_egress_client=None,
        _ANTIGRAVITY_EGRESS_CLIENT="stale-prepatch-client",
        _studio_orig_antigravity_egress_getter=None,  # Simulates early partial-import capture
        build_google_egress_client=lambda: "old-router-client",
        _get_antigravity_egress_client=orig_getter,
    )

    bridge._install_isolated_native_egress_bindings(fake_mod, ge)

    # Stale pre-patch client must be cleared immediately and getter tagged as studio native getter
    assert fake_mod._ANTIGRAVITY_EGRESS_CLIENT is None
    assert getattr(fake_mod._get_antigravity_egress_client, "_is_studio_native_egress_getter", False) is True

    # Production path: _get_antigravity_egress_client returns an isolated native client
    native_client = fake_mod._get_antigravity_egress_client()
    try:
        pool = native_client._transport._pool
        backend = pool._network_backend
        assert type(backend).__name__ == "_IsolatedNativeGoogleBackend"
    finally:
        asyncio.run(native_client.aclose())
        fake_mod._ANTIGRAVITY_EGRESS_CLIENT = None

    # Test monkeypatch of build_google_egress_client must still be honored
    sentinel = object()
    fake_mod.build_google_egress_client = lambda: sentinel
    assert fake_mod._get_antigravity_egress_client() is sentinel
    assert not orig_called

    # Test injection via google_egress_client must still take precedence
    injected = object()
    fake_mod.google_egress_client = injected
    assert fake_mod._get_antigravity_egress_client() is injected



