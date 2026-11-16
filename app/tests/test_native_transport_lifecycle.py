"""Lifecycle, phase-separation, non-replay, and resource cleanup contracts for Studio native transport."""
import asyncio
import importlib.util
import json
import pathlib
import re

import httpx


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

        _build_antigravity_native_url = staticmethod(lambda request: "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
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
        _build_antigravity_native_url = staticmethod(lambda request: "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
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
        _build_antigravity_native_url = staticmethod(lambda request: "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent")
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
