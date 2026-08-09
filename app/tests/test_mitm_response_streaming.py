import pytest

from app import mitm


class FakeRequest:
    def __init__(self, host, path):
        self.pretty_host = host
        self.path = path
        self.scheme = "https"
        self.host = host
        self.port = 443
        self.host_header = host
        self.method = "POST"


class FakeResponse:
    def __init__(self):
        self.stream = False
        self.http_version = "HTTP/2.0"
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}


class FakeFlow:
    def __init__(self, host, path):
        self.request = FakeRequest(host, path)
        self.response = FakeResponse()
        self.metadata = {}


def make_router(*, enabled=True, antigravity=True):
    router = object.__new__(mitm.BSLRouterMitm)
    router.config = {
        "mitm": {
            "enabled": enabled,
            "antigravity": antigravity,
            "copilot": True,
            "kiro": True,
        },
        "server": {"port": 6969},
    }
    router.load_config = lambda: None
    return router


def test_hijacked_stream_request_is_retargeted_marked_and_streamed():
    router = make_router()
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
    )

    router.request(flow)
    router.responseheaders(flow)

    assert flow.request.scheme == "http"
    assert flow.request.host == "127.0.0.1"
    assert flow.request.port == 6969
    assert flow.metadata["bsl_route_class"] == "chat"
    assert flow.metadata["bsl_chat_hijacked"] is True
    assert flow.metadata["bsl_chat_streaming"] is True
    assert flow.metadata["bsl_original_host"] == "daily-cloudcode-pa.googleapis.com"
    assert flow.metadata["bsl_original_path"] == "/v1beta/models/gemini-2.5-pro:streamGenerateContent"
    assert flow.metadata["bsl_antigravity_alias"] is None
    assert flow.metadata["bsl_alias_resolved"] is False
    assert callable(flow.response.stream)


def test_hijacked_non_stream_request_remains_buffered():
    router = make_router()
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-2.5-pro:generateContent?alt=json",
    )

    router.request(flow)
    router.responseheaders(flow)

    assert flow.request.scheme == "http"
    assert flow.request.host == "127.0.0.1"
    assert flow.request.port == 6969
    assert flow.metadata["bsl_route_class"] == "chat"
    assert flow.metadata["bsl_chat_hijacked"] is True
    assert flow.metadata["bsl_chat_streaming"] is False
    assert flow.metadata["bsl_original_host"] == "daily-cloudcode-pa.googleapis.com"
    assert flow.metadata["bsl_original_path"] == "/v1beta/models/gemini-2.5-pro:generateContent"
    assert flow.metadata["bsl_antigravity_alias"] is None
    assert flow.metadata["bsl_alias_resolved"] is False
    assert flow.response.stream is False


def test_alias_resolution_injects_both_alias_and_source_model_headers():
    """When MITM resolves an alias it must rewrite body.model and set both the
    alias header and the internal source-model header so main.py can attribute
    the original client model after the body rewrite."""
    router = make_router()
    router.config["antigravity_integration"] = {
        "enabled": True,
        "mappings": {"gemini-3.5-flash-low": "GLM-5.2"},
    }
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-3.5-flash-low:streamGenerateContent?alt=sse",
    )
    flow.request.content = b'{"model":"gemini-3.5-flash-low","request":{"contents":[]}}'
    flow.request.headers = {}

    router.request(flow)

    assert flow.request.headers["x-bsl-antigravity-alias"] == "GLM-5.2"
    assert flow.request.headers["x-bsl-antigravity-source-model"] == "gemini-3.5-flash-low"
    import json
    assert json.loads(flow.request.content)["model"] == "GLM-5.2"


def test_alias_resolution_without_body_still_sets_source_header():
    """Alias resolution must still set the source-model header even when there is
    no JSON body to rewrite (Gemini-path model extraction)."""
    router = make_router()
    router.config["antigravity_integration"] = {
        "enabled": True,
        "mappings": {"gemini-3.5-flash-low": "GLM-5.2"},
    }
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-3.5-flash-low:streamGenerateContent?alt=sse",
    )
    flow.request.content = b""
    flow.request.headers = {}

    router.request(flow)

    assert flow.request.headers["x-bsl-antigravity-alias"] == "GLM-5.2"
    assert flow.request.headers["x-bsl-antigravity-source-model"] == "gemini-3.5-flash-low"


def test_pass_through_request_is_retargeted_upstream_but_not_streamed(monkeypatch):
    router = make_router()
    monkeypatch.setattr(mitm, "_resolve_real_ip", lambda host: "203.0.113.42")
    flow = FakeFlow(
        "api.individual.githubcopilot.com",
        "/v1beta/projects/example:fetchAvailableModels",
    )

    router.request(flow)
    router.responseheaders(flow)

    assert flow.request.scheme == "https"
    assert flow.request.host == "203.0.113.42"
    assert flow.request.port == 443
    assert flow.request.host_header == "api.individual.githubcopilot.com"
    # Copilot fetchAvailableModels is a managed control_plane request: it is
    # retargeted to the real upstream (not hijacked as chat) and classified.
    assert flow.metadata.get("bsl_route_class") == "control_plane"
    assert flow.metadata.get("bsl_chat_hijacked") is not True
    assert flow.response.stream is False


def test_disabled_stream_request_is_neither_hijacked_nor_streamed():
    router = make_router(antigravity=False)
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
    )

    router.request(flow)
    router.responseheaders(flow)

    assert flow.request.scheme == "https"
    assert flow.request.host == "daily-cloudcode-pa.googleapis.com"
    assert flow.request.port == 443
    # Toggle off -> unrelated, untouched: no classification metadata attached.
    assert flow.metadata == {}
    assert flow.response.stream is False


@pytest.mark.parametrize(
    "host,path",
    [
        (
            "unmanaged.example.test",
            "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
        ),
        (
            "daily-cloudcode-pa.googleapis.com",
            "/v1beta/models/gemini-2.5-pro:streamGenerateContentExtra",
        ),
    ],
)
def test_unhijacked_stream_paths_cannot_activate_streaming(host, path):
    router = make_router()
    flow = FakeFlow(host, path)

    router.request(flow)
    router.responseheaders(flow)

    # Unmanaged host: untouched, empty metadata. Managed host whose path is not
    # a recognized chat verb: classified control_plane (not hijacked), so no
    # streaming callback is installed.
    if host == "unmanaged.example.test":
        assert flow.metadata == {}
    else:
        assert flow.metadata.get("bsl_route_class") == "control_plane"
        assert flow.metadata.get("bsl_chat_hijacked") is not True
    assert flow.response.stream is False


def test_responseheaders_ignores_missing_metadata_and_malformed_response():
    router = make_router()
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
    )

    del flow.metadata
    router.responseheaders(flow)
    assert flow.response.stream is False

    flow.metadata = {
        "bsl_chat_hijacked": True,
        "bsl_chat_streaming": True,
    }
    flow.response = None
    router.responseheaders(flow)



def test_server_connect_redirects_loopback_to_real_ip_and_preserves_sni(monkeypatch):
    router = make_router()
    monkeypatch.setattr(mitm, "_resolve_real_ip", lambda host: "203.0.113.42")
    data = type("HookData", (), {
        "server": type("Server", (), {"address": ("127.0.0.1", 443), "sni": None})(),
        "client": type("Client", (), {"sni": "api.individual.githubcopilot.com"})(),
    })()

    router.server_connect(data)

    assert data.server.address == ("203.0.113.42", 443)
    assert data.server.sni is None


def test_tls_start_server_uses_original_hostname_for_ip_destination():
    router = make_router()
    server = type("Server", (), {"sni": "203.0.113.42"})()
    tls_start = type("TlsStart", (), {
        "conn": server,
        "context": type("Context", (), {
            "client": type("Client", (), {"sni": "daily-cloudcode-pa.googleapis.com"})(),
        })(),
    })()

    router.tls_start_server(tls_start)

    assert server.sni == "daily-cloudcode-pa.googleapis.com"


def test_gemini_frame_shape_is_structural_and_redacts_generated_text():
    secret = "DO-NOT-PERSIST-THIS-TEXT"
    chunk = (
        'data: {"candidates":[{"content":{"parts":[{"text":"' + secret + '"}]},'
        '"finishReason":"STOP"}],"modelVersion":"gemini-test","responseId":"resp-1",'
        '"usageMetadata":{"promptTokenCount":1}}\n\ndata: [DONE]\n\n'
    ).encode("utf-8")

    shape = mitm._gemini_frame_shape(chunk)
    serialized = str(shape)

    assert shape["bytes"] == len(chunk)
    assert shape["sse_events"] == 2
    assert shape["done"] is True
    assert shape["frames"][0]["candidate_count"] == 1
    assert shape["frames"][0]["candidates"][0]["finish_reason"] == "STOP"
    assert shape["frames"][0]["has_model_version"] is True
    assert shape["frames"][0]["has_response_id"] is True
    assert secret not in serialized
    assert "text" in shape["frames"][0]["candidates"][0]["part_keys"][0]


def test_stream_callback_passes_bytes_through_and_emits_protocol_shape(monkeypatch):
    events = []
    monkeypatch.setattr(mitm, "_append_egress_telemetry", events.append)
    flow = FakeFlow(
        "daily-cloudcode-pa.googleapis.com",
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
    )
    callback = mitm._stream_telemetry_callback(flow)
    chunk = b'data: {"candidates":[]}\n\n'

    assert callback(chunk) == chunk
    assert events[0]["sequence"] == 1
    assert events[0]["http_version"] == "HTTP/2.0"
    assert events[0]["shape"]["frames"][0]["candidate_count"] == 0
