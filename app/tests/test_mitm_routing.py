"""Focused regression tests for MITM real-upstream recursion protection."""
import io
import json
import time
from types import SimpleNamespace

import pytest

from app import mitm


GOOGLE_HOST = "daily-cloudcode-pa.googleapis.com"


class FakeRequest:
    def __init__(self, host, path):
        self.pretty_host = host
        self.path = path
        self.scheme = "https"
        self.host = host
        self.port = 443
        self.host_header = host
        self.method = "POST"


class FakeFlow:
    def __init__(self, host, path):
        self.request = FakeRequest(host, path)
        self.metadata = {}
        self.response = None


def make_router(*, port=6969):
    router = object.__new__(mitm.BSLRouterMitm)
    router.config = {
        "mitm": {
            "enabled": True,
            "antigravity": True,
            "copilot": True,
            "kiro": True,
        },
        "server": {"port": port},
    }
    router.load_config = lambda: None
    return router


@pytest.fixture(autouse=True)
def isolate_real_upstream_state():
    cached = dict(mitm._REAL_IP_CACHE)
    last_known = dict(mitm._LAST_KNOWN_SAFE_REAL_IPS)
    mitm._REAL_IP_CACHE.clear()
    mitm._LAST_KNOWN_SAFE_REAL_IPS.clear()
    try:
        yield
    finally:
        mitm._REAL_IP_CACHE.clear()
        mitm._REAL_IP_CACHE.update(cached)
        mitm._LAST_KNOWN_SAFE_REAL_IPS.clear()
        mitm._LAST_KNOWN_SAFE_REAL_IPS.update(last_known)


def install_loopback_resolver(monkeypatch):
    class LoopbackResolver:
        def __init__(self, configure=False):
            self.nameservers = []
            self.lifetime = None

        def resolve(self, _host, _record_type):
            return [SimpleNamespace(address="127.0.0.1")]

    monkeypatch.setattr(mitm, "_HAS_DNS", True)
    monkeypatch.setattr(
        mitm,
        "dns",
        SimpleNamespace(resolver=SimpleNamespace(Resolver=LoopbackResolver)),
        raising=False,
    )


def test_loopback_resolver_output_is_rejected(monkeypatch):
    install_loopback_resolver(monkeypatch)

    assert mitm._resolve_real_ip(GOOGLE_HOST) is None
    assert GOOGLE_HOST not in mitm._REAL_IP_CACHE


def test_pass_through_fails_closed_instead_of_selecting_loopback(monkeypatch):
    router = make_router()
    monkeypatch.setattr(mitm, "_resolve_real_ip", lambda _host: "127.0.0.1")
    flow = FakeFlow("api.individual.githubcopilot.com", "/v1beta/projects/example:fetchAvailableModels")

    router.request(flow)

    assert flow.request.host == "api.individual.githubcopilot.com"
    assert flow.response.status_code == 502
    assert b"no safe real upstream IP" in flow.response.content


def test_last_validated_real_ip_survives_config_reload_and_loopback_answer(monkeypatch):
    safe_ip = "8.8.8.8"
    mitm._REAL_IP_CACHE[GOOGLE_HOST] = (safe_ip, time.time() - 1)
    mitm._LAST_KNOWN_SAFE_REAL_IPS[GOOGLE_HOST] = (safe_ip, time.time() + 60)

    router = object.__new__(mitm.BSLRouterMitm)
    router.config = {}
    monkeypatch.setattr(
        mitm,
        "open",
        lambda *_args, **_kwargs: io.StringIO("mitm:\n  enabled: true\n"),
        raising=False,
    )
    router.load_config()
    install_loopback_resolver(monkeypatch)

    assert mitm._resolve_real_ip(GOOGLE_HOST) == safe_ip
    assert mitm._REAL_IP_CACHE[GOOGLE_HOST][0] == safe_ip


def test_server_connect_blocks_unresolved_loopback_recursion(monkeypatch):
    router = make_router()
    monkeypatch.setattr(mitm, "_resolve_real_ip", lambda _host: None)
    data = SimpleNamespace(
        server=SimpleNamespace(address=("127.0.0.1", 443), sni=None),
        client=SimpleNamespace(sni="api.individual.githubcopilot.com"),
    )

    router.server_connect(data)

    assert data.server.address == ("127.0.0.1", 443)
    assert data.server.error.msg.startswith("BSL MITM: no safe")


def test_chat_path_still_routes_only_to_bsl_listener():
    router = make_router(port=6969)
    flow = FakeFlow(GOOGLE_HOST, "/v1beta/models/gemini:streamGenerateContent?alt=sse")

    router.request(flow)

    assert flow.request.scheme == "http"
    assert flow.request.host == "127.0.0.1"
    assert flow.request.port == 6969


def test_mitm_source_does_not_weaken_tls_verification():
    source = __import__("pathlib").Path("app/mitm.py").read_text(encoding="utf-8")

    assert "verify=False" not in source
    assert "ssl_insecure" not in source


# ─────────────────────────────────────────────────────────────────────────────
# Google quota-drain regression (plan: google-quota-drain-plan.md)
# These prove the MITM classification boundary preserves control-plane
# pass-through, keeps chat local, leaves unrelated traffic untouched, and emits
# no secret-bearing telemetry. The actual native-Google inference decision lives
# in main.py (owned by the 429-freeze task); MITM only records a redacted
# route-class label and whether a BSL alias resolved.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def capture_route_decisions(monkeypatch):
    events = []
    monkeypatch.setattr(mitm, "_emit_route_decision", events.append)
    return events


def test_control_plane_request_stays_passthrough_to_bsl_gateway(capture_route_decisions):
    """A non-inference /v1internal:* RPC must route to BSL Router (which forwards
    credentials to real Google), NOT be hijacked as chat and NOT be dropped."""
    router = make_router(port=6969)
    flow = FakeFlow(GOOGLE_HOST, "/v1internal:loadCodeAssist")

    router.request(flow)

    # Control-plane traffic on an Antigravity domain is retargeted to BSL Router
    # so its CCPA gateway can forward auth/quota/model-discovery to real Google.
    assert flow.request.scheme == "http"
    assert flow.request.host == "127.0.0.1"
    assert flow.request.port == 6969
    assert flow.metadata["bsl_route_class"] == "control_plane"
    assert flow.metadata.get("bsl_chat_hijacked") is not True
    assert flow.metadata.get("bsl_alias_resolved") is False
    # The route decision labels it control_plane (credential forwarding), not chat.
    assert capture_route_decisions[0]["route_class"] == "control_plane"


def test_chat_request_stays_local_bsl_interception(capture_route_decisions):
    """A recognized inference verb must be classified chat and routed to BSL
    Router, with alias resolution outcome recorded for auditability."""
    router = make_router(port=6969)
    flow = FakeFlow(
        GOOGLE_HOST,
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
    )
    flow.request.headers = {}
    flow.request.content = b""

    router.request(flow)

    assert flow.request.scheme == "http"
    assert flow.request.host == "127.0.0.1"
    assert flow.request.port == 6969
    assert flow.metadata["bsl_route_class"] == "chat"
    assert flow.metadata["bsl_chat_hijacked"] is True
    # Unmapped chat: alias did not resolve; main.py's native path is the only
    # remaining option. MITM records this without itself calling native Google.
    assert flow.metadata["bsl_alias_resolved"] is False
    assert capture_route_decisions[0]["route_class"] == "chat"


def test_chat_request_with_alias_marks_alias_resolved(capture_route_decisions):
    """A mapped chat request must record alias_resolved=True so the audit trail
    can distinguish BSL-bound inference from native-bound inference."""
    router = make_router(port=6969)
    router.config["antigravity_integration"] = {
        "enabled": True,
        "mappings": {"gemini-3.5-flash-low": "GLM-5.2"},
    }
    flow = FakeFlow(
        GOOGLE_HOST,
        "/v1beta/models/gemini-3.5-flash-low:streamGenerateContent?alt=sse",
    )
    flow.request.headers = {}
    flow.request.content = b'{"model":"gemini-3.5-flash-low","request":{"contents":[]}}'

    router.request(flow)

    assert flow.metadata["bsl_route_class"] == "chat"
    assert flow.metadata["bsl_alias_resolved"] is True
    assert flow.request.headers["x-bsl-antigravity-alias"] == "GLM-5.2"


def test_unrelated_traffic_remains_untouched(capture_route_decisions):
    """A host MITM does not manage must be left completely alone: no retarget,
    no metadata, only an unrelated route-decision event."""
    router = make_router(port=6969)
    flow = FakeFlow("unmanaged.example.test", "/v1beta/models/x:generateContent")

    router.request(flow)

    assert flow.request.scheme == "https"
    assert flow.request.host == "unmanaged.example.test"
    assert flow.request.port == 443
    assert flow.metadata == {}
    assert len(capture_route_decisions) == 1
    assert capture_route_decisions[0]["route_class"] == "unrelated"


def test_managed_host_with_toggle_off_is_unrelated(capture_route_decisions):
    """When the Antigravity toggle is off, a normally-managed host must be
    treated as unrelated and untouched (not hijacked to BSL)."""
    router = make_router(port=6969)
    router.config["mitm"]["antigravity"] = False
    flow = FakeFlow(GOOGLE_HOST, "/v1beta/models/gemini:streamGenerateContent?alt=sse")

    router.request(flow)

    assert flow.request.scheme == "https"
    assert flow.request.host == GOOGLE_HOST
    assert flow.metadata == {}
    assert capture_route_decisions[0]["route_class"] == "unrelated"


def test_route_decision_telemetry_carries_no_secrets(capture_route_decisions, monkeypatch):
    """Route-decision telemetry must never include Authorization, cookies,
    tokens, the query string, or the request body — only structural fields."""
    router = make_router(port=6969)
    flow = FakeFlow(
        GOOGLE_HOST,
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse&token=SECRET-TOK",
    )
    flow.request.headers = {
        "authorization": "Bearer BEARER-SECRET",
        "cookie": "session=COOKIE-SECRET",
        "x-goog-api-key": "API-KEY-SECRET",
        "x-api-key": "XAPI-SECRET",
    }
    flow.request.content = b'{"model":"gemini-2.5-pro","contents":[{"parts":[{"text":"PROMPT-SECRET"}]}]}'

    emitted = []
    monkeypatch.setattr(mitm, "_emit_route_decision", emitted.append)
    router.request(flow)

    assert emitted, "expected a route-decision event"
    blob = json.dumps(emitted, default=str)
    for secret in ("BEARER-SECRET", "COOKIE-SECRET", "API-KEY-SECRET", "XAPI-SECRET", "PROMPT-SECRET", "SECRET-TOK"):
        assert secret not in blob, f"route-decision telemetry leaked {secret!r}"
    # The query string (which may carry tokens) must not appear verbatim.
    for ev in emitted:
        assert "alt=sse" not in ev.get("path", "")
        assert "token=" not in ev.get("path", "")
    # And only allowlisted structural keys are present.
    allowed = {"ts", "event", "route_class", "host", "path", "method", "alias_resolved"}
    for ev in emitted:
        assert set(ev.keys()).issubset(allowed | {"status_code"})


def test_responseheaders_emits_route_decision_with_status(capture_route_decisions):
    """responseheaders must emit a route-decision event with the observed status
    for both chat and control_plane managed flows."""
    router = make_router(port=6969)

    chat_flow = FakeFlow(
        GOOGLE_HOST,
        "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
    )
    chat_flow.request.headers = {}
    chat_flow.request.content = b""
    chat_flow.response = type("R", (), {"status_code": 200, "headers": {}, "http_version": "HTTP/2.0", "stream": False})()
    router.request(chat_flow)
    router.responseheaders(chat_flow)

    ccpa_flow = FakeFlow(GOOGLE_HOST, "/v1internal:fetchAvailableModels")
    ccpa_flow.response = type("R", (), {"status_code": 200, "headers": {}, "http_version": "HTTP/2.0", "stream": False})()
    router.request(ccpa_flow)
    router.responseheaders(ccpa_flow)

    decision_events = [e for e in capture_route_decisions if e.get("event") == "route_decision"]
    classes = {e["route_class"] for e in decision_events}
    assert "chat" in classes and "control_plane" in classes
    # The responseheaders-emitted event carries the observed status_code; the
    # request-time event (emitted before any response) intentionally omits it.
    status_events = [e for e in decision_events if "status_code" in e]
    status_classes = {e["route_class"] for e in status_events}
    assert "chat" in status_classes and "control_plane" in status_classes
    for e in status_events:
        assert e["status_code"] == 200


def test_classify_route_helper_labels_without_secrets():
    """The classifier uses only host/path/chat-verb; no payload or headers."""
    assert mitm._classify_route(GOOGLE_HOST, "/v1beta/models/x:generateContent", True, True) == "chat"
    assert mitm._classify_route(GOOGLE_HOST, "/v1internal:loadCodeAssist", False, True) == "control_plane"
    assert mitm._classify_route("unmanaged.example.test", "/anything", True, False) == "unrelated"


def test_redact_path_strips_query_string():
    assert mitm._redact_path("/v1beta/models/x:generateContent?alt=sse&token=z") == "/v1beta/models/x:generateContent"
    assert mitm._redact_path("") == ""
    assert mitm._redact_path("/v1internal:loadCodeAssist") == "/v1internal:loadCodeAssist"
