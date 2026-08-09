"""Focused CCPA control-plane gateway tests; no real upstream calls."""
import asyncio
import builtins
import io
import json
import os
import sys

import httpx
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _NativeClient:
    def __init__(self, response):
        self.response = response
        self.request = None
        self.stream = None

    def build_request(self, method, url, **kwargs):
        self.request = httpx.Request(method, url, **kwargs)
        return self.request

    async def send(self, request, stream=False):
        self.stream = stream
        return self.response


class _UnavailableClient:
    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        raise httpx.ConnectError("upstream unavailable", request=request)


def _request(path, body=b'{"request":"control"}', headers=None, query=b"alt=sse&client=antigravity"):
    delivered = False
    header_pairs = [(b"content-type", b"application/json")]
    for name, value in (headers or {}).items():
        header_pairs.append((name.encode("latin-1"), value.encode("latin-1")))

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": query,
            "headers": header_pairs,
            "scheme": "http",
            "server": ("127.0.0.1", 6969),
            "client": ("127.0.0.1", 50000),
        },
        receive,
    )


def _integration_config():
    return {
        "server": {"port": 6969},
        "providers": {},
        "combos": [{"alias": "bsl-target", "chain": []}],
        "aliases": {},
        "antigravity_integration": {
            "enabled": True,
            "mappings": {"gemini-3.5-flash-low": "bsl-target"},
        },
    }


def _block_forensics(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        normalized = str(path).replace("\\", "/")
        if normalized.endswith(".brain/logs/antigravity_inbound.jsonl"):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)


async def _consume_stream(response):
    chunks = [chunk async for chunk in response.body_iterator]
    if response.background is not None:
        await response.background()
    return chunks


async def _call_control(operation, request):
    return await main.antigravity_ccpa_control_proxy(request, operation)


def test_inference_routes_precede_ccpa_catch_all_and_remain_local(monkeypatch):
    forwarded = []
    local_calls = []
    _block_forensics(monkeypatch)

    async def local_process(body, **kwargs):
        local_calls.append((body, kwargs))
        return Response(b"local")

    async def forbidden_forward(*_args, **_kwargs):
        forwarded.append(True)
        raise AssertionError("chat route must not enter the CCPA control-plane proxy")

    cs.replace_config(_integration_config())
    monkeypatch.setattr(main, "_process_chat_completion", local_process)
    monkeypatch.setattr(main, "_forward_antigravity_ccpa_control", forbidden_forward)

    chat_paths = [route.path for route in main.app.routes]
    assert chat_paths.index("/v1internal:generateContent") < chat_paths.index("/v1internal:{operation}")
    assert chat_paths.index("/v1internal:streamGenerateContent") < chat_paths.index("/v1internal:{operation}")

    body = json.dumps({
        "project": "test-project",
        "model": "gemini-3.5-flash-low",
        "userAgent": "antigravity",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    }).encode("utf-8")
    response = asyncio.run(main.antigravity_generate(_request("/v1internal:generateContent", body)))

    assert response.body == b"local"
    assert local_calls
    assert forwarded == []


async def _forward_control_with_response(operation):
    stream = _BytesStream([b'{"ok":', b"true}"])
    upstream = httpx.Response(
        207,
        headers={
            "content-type": "application/json",
            "x-goog-request-id": "google-request",
            "connection": "close",
            "content-length": "11",
        },
        stream=stream,
    )
    client = _NativeClient(upstream)
    request = _request(
        f"/v1internal:{operation}",
        body=b'{"project":"test-project"}',
        headers={
            "authorization": "Bearer google-secret",
            "x-goog-api-key": "api-key-secret",
            "x-goog-user-project": "test-project",
            "x-client-data": "client-data",
            "cookie": "session=do-not-forward",
            "connection": "keep-alive",
            "x-unrelated": "do-not-forward",
        },
    )
    previous_egress = main.google_egress_client
    main.google_egress_client = client
    try:
        response = await _call_control(operation, request)
        chunks = await _consume_stream(response)
    finally:
        main.google_egress_client = previous_egress
    return response, chunks, stream, client


def test_ccpa_control_operations_forward_google_headers_query_status_and_body():
    for operation in ("loadCodeAssist", "fetchAvailableModels", "fetchUserInfo"):
        response, chunks, stream, client = asyncio.run(_forward_control_with_response(operation))

        assert response.status_code == 207
        assert b"".join(chunks) == b'{"ok":true}'
        assert response.headers["content-type"] == "application/json"
        assert response.headers["x-goog-request-id"] == "google-request"
        assert "connection" not in response.headers
        assert "content-length" not in response.headers
        assert stream.closed is True
        assert client.stream is True
        assert client.request.method == "POST"
        assert client.request.url.host == "cloudcode-pa.googleapis.com"
        assert client.request.url.path == f"/v1internal:{operation}"
        assert client.request.url.query == b"alt=sse&client=antigravity"
        assert client.request.content == b'{"project":"test-project"}'
        assert client.request.headers["authorization"] == "Bearer google-secret"
        assert client.request.headers["x-goog-api-key"] == "api-key-secret"
        assert client.request.headers["x-goog-user-project"] == "test-project"
        assert client.request.headers["x-client-data"] == "client-data"
        assert "cookie" not in client.request.headers
        assert "connection" not in client.request.headers
        assert "x-unrelated" not in client.request.headers


def test_ccpa_proxy_rejects_local_recursion_without_upstream_request(monkeypatch):
    client = _NativeClient(httpx.Response(200, content=b"unexpected"))
    monkeypatch.setattr(main, "google_egress_client", client)
    monkeypatch.setattr(main, "_ANTIGRAVITY_CCPA_BASE_URL", "http://127.0.0.1:6969")

    response = asyncio.run(_call_control(
        "loadCodeAssist",
        _request("/v1internal:loadCodeAssist", headers={"authorization": "Bearer google-secret"}),
    ))

    assert response.status_code == 502
    error = json.loads(response.body)["error"]
    assert error["status"] == "UNSAFE_UPSTREAM"
    assert "approved Google Cloud Code HTTPS origin" in error["message"]
    assert client.request is None


def test_ccpa_proxy_does_not_log_credentials_when_upstream_is_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(main, "google_egress_client", _UnavailableClient())

    response = asyncio.run(_call_control(
        "loadCodeAssist",
        _request(
            "/v1internal:loadCodeAssist",
            body=b'{"token":"body-secret"}',
            headers={"authorization": "Bearer header-secret", "x-goog-api-key": "key-secret"},
        ),
    ))

    captured = capsys.readouterr().out
    assert response.status_code == 502
    assert json.loads(response.body)["error"]["status"] == "UPSTREAM_UNAVAILABLE"
    assert "header-secret" not in captured
    assert "key-secret" not in captured
    assert "body-secret" not in captured


def test_ccpa_proxy_rejects_non_control_operation(monkeypatch):
    client = _NativeClient(httpx.Response(200, content=b"unexpected"))
    monkeypatch.setattr(main, "google_egress_client", client)

    response = asyncio.run(_call_control(
        "invalid-operation!",
        _request("/v1internal:invalid-operation!"),
    ))

    assert response.status_code == 404
    assert json.loads(response.body)["error"]["status"] == "UNSUPPORTED_CONTROL_OPERATION"
    assert client.request is None


def test_ccpa_proxy_requires_google_credentials(monkeypatch):
    client = _NativeClient(httpx.Response(200, content=b"unexpected"))
    monkeypatch.setattr(main, "google_egress_client", client)

    response = asyncio.run(_call_control(
        "loadCodeAssist",
        _request("/v1internal:loadCodeAssist"),
    ))

    assert response.status_code == 401
    assert json.loads(response.body)["error"]["status"] == "UNAUTHENTICATED"
    assert client.request is None


class _ValueErrorClient:
    def __init__(self, stage):
        self.stage = stage

    def build_request(self, method, url, **kwargs):
        if self.stage == "request-build":
            raise ValueError("request build failure")
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        if self.stage == "upstream-send":
            raise ValueError("upstream send failure")
        return httpx.Response(200, stream=_BytesStream([b"ok"]))


async def _runtime_ccpa_handler(request):
    body = await request.aread()
    return httpx.Response(
        207,
        headers={"content-type": "application/json", "x-goog-request-id": "runtime-request"},
        stream=_BytesStream([body]),
        request=request,
    )


def test_ccpa_downstream_value_errors_are_not_mislabeled_as_unsafe_upstream(monkeypatch, capsys):
    for stage in ("request-build", "upstream-send", "response-headers"):
        client = _ValueErrorClient(stage)
        monkeypatch.setattr(main, "google_egress_client", client)
        if stage == "response-headers":
            monkeypatch.setattr(
                main,
                "_antigravity_native_response_headers",
                lambda _response: (_ for _ in ()).throw(ValueError("header conversion failure")),
            )

        response = asyncio.run(_call_control(
            "loadCodeAssist",
            _request(
                "/v1internal:loadCodeAssist",
                body=b'{"token":"body-secret"}',
                headers={"authorization": "Bearer header-secret"},
            ),
        ))

        error = json.loads(response.body)["error"]
        assert response.status_code == 502
        assert error["status"] == "UPSTREAM_UNAVAILABLE"
        assert error["status"] != "UNSAFE_UPSTREAM"
        captured = capsys.readouterr().out
        assert f"operation=loadCodeAssist stage={stage} error=ValueError" in captured
        assert "header-secret" not in captured
        assert "body-secret" not in captured


def test_ccpa_runtime_lifespan_shared_client_forwards_via_exact_route(monkeypatch):
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_runtime_ccpa_handler))
    previous_client = main.http_client
    previous_egress = main.google_egress_client

    def load_test_config():
        cs.replace_config(_integration_config())

    monkeypatch.setattr(main, "load_config", load_test_config)
    monkeypatch.setattr(main, "_build_hardened_client", lambda proxy_url=None: shared_client)
    # CCPA control now forwards via the dedicated egress client; point its builder
    # at the same in-memory mock transport so the lifespan stays hermetic (no real
    # network) and does not leave a live client in the module global for later tests.
    monkeypatch.setattr(main, "build_google_egress_client", lambda: shared_client)
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1internal:loadCodeAssist?alt=sse&client=antigravity",
                content=b'{"project":"runtime-test"}',
                headers={
                    "authorization": "Bearer test-credential",
                    "accept-encoding": "gzip, deflate, br",
                },
            )
    finally:
        main.http_client = previous_client
        main.google_egress_client = previous_egress

    assert response.status_code == 207
    assert response.content == b'{"project":"runtime-test"}'
    assert response.headers["x-goog-request-id"] == "runtime-request"


def test_ccpa_raw_streaming_preserves_accept_encoding_without_decoding(monkeypatch):
    captured = {}

    async def handler(request):
        captured["request"] = request
        return httpx.Response(
            207,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=_BytesStream([b"raw-compressed-response"]),
            request=request,
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(main, "google_egress_client", client)
        try:
            response = await _call_control(
                "loadCodeAssist",
                _request(
                    "/v1internal:loadCodeAssist",
                    headers={
                        "authorization": "Bearer test-credential",
                        "accept-encoding": "gzip, deflate, br",
                    },
                ),
            )
            return response, await _consume_stream(response)
        finally:
            await client.aclose()

    response, chunks = asyncio.run(scenario())

    assert captured["request"].headers["accept-encoding"] == "gzip, deflate, br"
    assert response.headers["content-encoding"] == "gzip"
    assert chunks == [b"raw-compressed-response"]


# ─────────────────────────────────────────────────────────────────────────────
# Dedicated Google egress client wiring (hosts-file-bypass transport).
# These lock in that BOTH Google forwarding paths use main.google_egress_client
# when it is set, that the general proxy client is NOT used as a fallback in
# that case, that the 502 error contract is preserved on egress failure, and
# that the upstream stream is closed exactly once if header conversion fails
# after a successful streaming send (resource-leak regression).
# ─────────────────────────────────────────────────────────────────────────────
class _AcloseTrackingResponse:
    """Stand-in upstream response that counts aclose() awaits."""

    def __init__(self, status_code=207):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.aclose_count = 0

    async def aiter_raw(self):  # pragma: no cover - never reached in leak path
        if False:
            yield b""

    async def aclose(self):
        self.aclose_count += 1


class _StreamOnlyClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    def build_request(self, method, url, **kwargs):
        self.request = httpx.Request(method, url, **kwargs)
        return self.request

    async def send(self, request, stream=False):
        return self.response


def test_ccpa_control_uses_dedicated_egress_client_not_proxy(monkeypatch):
    stream = _BytesStream([b'{"ok":', b"true}"])
    upstream = httpx.Response(207, headers={"content-type": "application/json"}, stream=stream)
    egress_client = _NativeClient(upstream)
    proxy_client = _NativeClient(httpx.Response(200, content=b"proxy-should-not-run"))

    monkeypatch.setattr(main, "google_egress_client", egress_client)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda proxy_url=None: proxy_client)

    async def scenario():
        response = await _call_control(
            "loadCodeAssist",
            _request(
                "/v1internal:loadCodeAssist",
                body=b'{"project":"p"}',
                headers={"authorization": "Bearer google-secret"},
            ),
        )
        return response, await _consume_stream(response)

    response, chunks = asyncio.run(scenario())

    assert response.status_code == 207
    assert b"".join(chunks) == b'{"ok":true}'
    assert egress_client.request is not None
    assert egress_client.request.url.host == "cloudcode-pa.googleapis.com"
    assert egress_client.stream is True
    # The general proxy client must NOT be consulted when the egress client exists.
    assert proxy_client.request is None


def test_native_forward_uses_dedicated_egress_client_not_proxy(monkeypatch):
    stream = _BytesStream([b"native-ok"])
    upstream = httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
    egress_client = _NativeClient(upstream)
    proxy_client = _NativeClient(httpx.Response(200, content=b"proxy-should-not-run"))

    monkeypatch.setattr(main, "google_egress_client", egress_client)
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda proxy_url=None: proxy_client)

    async def scenario():
        response = await main._forward_antigravity_native(
            _request(
                "/v1internal:streamGenerateContent",
                headers={"authorization": "Bearer google-secret"},
            ),
            True,
            b'{"q":1}',
        )
        return response, await _consume_stream(response)

    response, chunks = asyncio.run(scenario())

    assert response.status_code == 200
    assert b"".join(chunks) == b"native-ok"
    assert egress_client.request is not None
    assert egress_client.request.url.host == "daily-cloudcode-pa.googleapis.com"
    assert proxy_client.request is None


def test_ccpa_egress_connect_failure_preserves_502_contract(monkeypatch, capsys):
    # A resolution/connect failure inside the dedicated egress client must still
    # produce the unchanged UPSTREAM_UNAVAILABLE contract with no credential leak.
    monkeypatch.setattr(main, "google_egress_client", _UnavailableClient())

    response = asyncio.run(_call_control(
        "loadCodeAssist",
        _request(
            "/v1internal:loadCodeAssist",
            body=b'{"token":"body-secret"}',
            headers={"authorization": "Bearer header-secret", "x-goog-api-key": "key-secret"},
        ),
    ))

    captured = capsys.readouterr().out
    assert response.status_code == 502
    assert json.loads(response.body)["error"]["status"] == "UPSTREAM_UNAVAILABLE"
    assert "header-secret" not in captured
    assert "key-secret" not in captured
    assert "body-secret" not in captured


def test_ccpa_stream_leak_closes_upstream_on_header_error(monkeypatch, capsys):
    resp = _AcloseTrackingResponse(207)
    monkeypatch.setattr(main, "google_egress_client", _StreamOnlyClient(resp))
    monkeypatch.setattr(
        main,
        "_antigravity_native_response_headers",
        lambda _response: (_ for _ in ()).throw(ValueError("header conversion failure")),
    )

    response = asyncio.run(_call_control(
        "loadCodeAssist",
        _request("/v1internal:loadCodeAssist", headers={"authorization": "Bearer header-secret"}),
    ))

    assert response.status_code == 502
    assert json.loads(response.body)["error"]["status"] == "UPSTREAM_UNAVAILABLE"
    assert resp.aclose_count == 1, "upstream stream must be closed exactly once on header error"
    captured = capsys.readouterr().out
    assert "header-secret" not in captured


def test_native_stream_leak_closes_upstream_on_header_error(monkeypatch):
    resp = _AcloseTrackingResponse(200)
    monkeypatch.setattr(main, "google_egress_client", _StreamOnlyClient(resp))
    monkeypatch.setattr(
        main,
        "_antigravity_native_response_headers",
        lambda _response: (_ for _ in ()).throw(ValueError("header conversion failure")),
    )

    response = asyncio.run(main._forward_antigravity_native(
        _request("/v1internal:streamGenerateContent", headers={"authorization": "Bearer x"}),
        True,
        b'{"q":1}',
    ))

    # Streaming path returns 200 with Gemini SSE error body (correct for Antigravity IDE).
    # The critical invariant is that the upstream stream is closed exactly once (no leak).
    assert response.status_code == 200
    assert b"UNAVAILABLE" in response.body or b"error" in response.body
    assert resp.aclose_count == 1, "upstream stream must be closed exactly once on header error"
