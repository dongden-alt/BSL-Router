"""Focused direct Antigravity integration overlay tests; no real upstream calls."""
import asyncio
import builtins
import io
import json
import os
import re
import sys

import httpx
import pytest
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.config_state as cs
import app.main as main


EXPECTED_AG_SLOTS = (
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-low",
    "gemini-3.1-pro-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)
EXPECTED_AG_LABELS = (
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
)
REMOVED_AG_SLOTS = (
    "gemini-3-flash-agent",
    "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-request-antigravity",
    "gemini-default",
    "gemini-3-flash",
)


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

    def build_request(self, method, url, **kwargs):
        self.request = httpx.Request(method, url, **kwargs)
        return self.request

    async def send(self, request, stream=False):
        return self.response


def _request(path, body, headers=None, raw_body=None):
    encoded = json.dumps(body).encode("utf-8") if raw_body is None else raw_body
    delivered = False
    header_pairs = [(b"content-type", b"application/json")]
    for name, value in (headers or {}).items():
        header_pairs.append((name.encode("latin-1"), value.encode("latin-1")))

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
            "path": path,
            "query_string": b"alt=sse",
            "headers": header_pairs,
            "scheme": "http",
            "server": ("127.0.0.1", 6969),
            "client": ("127.0.0.1", 50000),
        },
        receive,
    )


def _body(model="gemini-3.5-flash-low"):
    return {
        "project": "test-project",
        "requestId": "test-request-id",
        "model": model,
        "userAgent": "antigravity",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    }


def _config(enabled=True, mappings=None):
    return {
        "server": {"port": 6969},
        "providers": {
            "fake": {
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://fake.invalid"}],
                "models": [{"id": "fake-model", "enabled": True}, {"id": "claude-sonnet-5-thinking", "enabled": True}],
            },
            "pix4k": {
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://fake.invalid"}],
                "models": [{"id": "claude-sonnet-5-thinking", "enabled": True}],
            }
        },
        "combos": [
            {"alias": "coder-1", "chain": ["fake/fake-model"]},
            {"alias": "coder-2", "chain": ["fake/fake-model"]}
        ],
        "aliases": {},
        "antigravity_integration": {"enabled": enabled, "mappings": mappings or {}},
    }


def _block_forensics(monkeypatch):
    real_open = builtins.open

    def open_without_forensics(path, *args, **kwargs):
        normalized = str(path).replace("\\", "/")
        if normalized.endswith((".brain/logs/antigravity_inbound.jsonl", ".brain/logs/outbound_upstream.jsonl")):
            return io.StringIO()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_without_forensics)


def test_exact_antigravity_211_slot_contract_is_ordered_and_exclusive():
    assert main.ANTIGRAVITY_INTEGRATION_SLOTS == EXPECTED_AG_SLOTS
    assert not set(EXPECTED_AG_SLOTS).intersection(REMOVED_AG_SLOTS)


def test_dedicated_mapping_validation_accepts_combo_and_provider_and_drops_obsolete_slots():
    config = _config(mappings={
        "gemini-3.5-flash-low": "coder-1",
        "gemini-3.1-pro-low": "fake/fake-model",
        "gemini-default": "coder-1",
    })

    validated = main._validate_antigravity_integration_config(config)

    assert validated["antigravity_integration"] == {
        "enabled": True,
        "mappings": {
            "gemini-3.5-flash-low": "coder-1",
            "gemini-3.1-pro-low": "fake/fake-model",
        },
    }


def test_dedicated_mapping_validation_migrates_known_high_alias_and_current_key_wins():
    migrated = main._validate_antigravity_integration_config(_config(mappings={
        "gemini-3-flash-agent": "fake/fake-model",
    }))
    preferred = main._validate_antigravity_integration_config(_config(mappings={
        "gemini-3-flash-agent": "fake/fake-model",
        "gemini-3.5-flash-high": "coder-1",
    }))

    assert migrated["antigravity_integration"]["mappings"] == {
        "gemini-3.5-flash-high": "fake/fake-model",
    }
    assert preferred["antigravity_integration"]["mappings"] == {
        "gemini-3.5-flash-high": "coder-1",
    }


def test_dedicated_mapping_validation_drops_unknown_slots_and_dead_targets(capsys):
    # Unknown source slot: silently dropped with a console warning.
    validated = main._validate_antigravity_integration_config(_config(mappings={
        "unknown-slot": "coder-1",
    }))
    assert validated["antigravity_integration"]["mappings"] == {}
    captured = capsys.readouterr()
    assert "Dropping unknown source slot" in captured.out
    assert "unknown-slot" in captured.out

    # Dead mapping target: silently dropped with a console warning.
    validated = main._validate_antigravity_integration_config(_config(mappings={
        "gemini-3.5-flash-low": "missing/provider",
    }))
    assert validated["antigravity_integration"]["mappings"] == {}
    captured = capsys.readouterr()
    assert "Dropping dead mapping target" in captured.out
    assert "missing/provider" in captured.out


def test_start_stop_persist_direct_integration_state_without_mitm(monkeypatch):
    saved = []
    cs.replace_config(_config(enabled=False))
    monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: saved.append(cfg))

    started = asyncio.run(main.antigravity_integration_start())
    stopped = asyncio.run(main.antigravity_integration_stop())

    assert json.loads(started.body)["enabled"] is True
    assert json.loads(stopped.body)["enabled"] is False
    assert [snapshot["antigravity_integration"]["enabled"] for snapshot in saved] == [True, False]
    assert all("mitm" not in snapshot for snapshot in saved)


def test_config_post_persists_validated_dedicated_mappings(monkeypatch):
    saved = []
    candidate = _config(mappings={"gemini-3.5-flash-low": "coder-1"})
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: saved.append(cfg))

    response = asyncio.run(main.update_config(_request("/api/config", candidate)))

    assert response.status_code == 200
    assert saved[-1]["antigravity_integration"] == {
        "enabled": True,
        "mappings": {"gemini-3.5-flash-low": "coder-1"},
    }


def test_config_post_drops_invalid_dedicated_mapping_silently(monkeypatch, capsys):
    saved = []
    candidate = _config(mappings={"unknown-slot": "coder-1"})
    cs.replace_config(_config())
    monkeypatch.setattr(main, "_persist_config_snapshot", lambda cfg: saved.append(cfg))

    response = asyncio.run(main.update_config(_request("/api/config", candidate)))

    assert response.status_code == 200
    captured = capsys.readouterr()
    assert "Dropping unknown source slot" in captured.out
    assert saved[-1]["antigravity_integration"] == {
        "enabled": True,
        "mappings": {},
    }


def test_stopped_or_unmapped_requests_select_native_fallback_when_credentials_are_present(monkeypatch):
    calls = []

    async def native(request, is_stream, raw_body):
        calls.append((is_stream, raw_body))
        return Response(b"native")

    async def mapped(*_args, **_kwargs):
        raise AssertionError("mapped resolver must not run")

    cs.replace_config(_config(enabled=False, mappings={}))
    monkeypatch.setattr(main, "_forward_antigravity_native", native)
    monkeypatch.setattr(main, "_process_chat_completion", mapped)
    response = asyncio.run(main.antigravity_generate(_request(
        "/v1internal:streamGenerateContent", _body(), {"authorization": "Bearer test"},
    )))

    assert response.body == b"native"
    assert calls and calls[0][0] is True

    cs.replace_config(_config(enabled=True, mappings={}))
    response = asyncio.run(main.antigravity_generate(_request(
        "/v1internal:generateContent", _body(), {"x-goog-api-key": "test"},
    )))
    assert response.body == b"native"
    assert calls[-1][0] is False


def test_unmapped_live_envelope_without_google_credentials_returns_parseable_sse_error(monkeypatch):
    native_calls = []

    async def native(*_args, **_kwargs):
        native_calls.append(True)
        raise AssertionError("credential-less request must not reach native upstream")

    cs.replace_config(_config(enabled=True, mappings={}))
    monkeypatch.setattr(main, "_forward_antigravity_native", native)

    response = asyncio.run(main.antigravity_generate(
        _request("/v1internal:streamGenerateContent", _body("gemini-3.5-flash-low")),
    ))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line[6:])
        for line in response.body.splitlines()
        if line.startswith(b"data: ") and line != b"data: [DONE]"
    ]
    # FREEZE FIX (2026-08-07): the terminal contract is now a SOLE
    # finishReason-bearing candidate frame — NO preceding bare {"error":...}
    # frame. The old error -> terminal -> [DONE] sequence poisoned the
    # Antigravity Gemini parser (a top-level error object makes it stop
    # consuming the later finishReason candidate), freezing the IDE on
    # 2026-08-07. The 401 message stays VISIBLE in the candidate's parts text.
    assert len(frames) == 1, f"expected a single terminal frame, got {len(frames)}"
    candidate = frames[0]["response"]["candidates"][0]
    assert candidate["finishReason"] == "STOP", "terminal frame must carry a finishReason"
    _text = candidate["content"]["parts"][0]["text"]
    assert "401" in _text
    assert "gemini-3.5-flash-low" in _text
    assert "unmapped" in _text
    assert "credentials were not forwarded" in _text
    assert response.body.endswith(main.GEMINI_SSE_DONE)
    assert native_calls == []


def test_unmapped_live_envelope_without_google_credentials_returns_structured_nonstream_error(monkeypatch):
    native_calls = []

    async def native(*_args, **_kwargs):
        native_calls.append(True)
        raise AssertionError("credential-less request must not reach native upstream")

    cs.replace_config(_config(enabled=True, mappings={}))
    monkeypatch.setattr(main, "_forward_antigravity_native", native)

    response = asyncio.run(main.antigravity_generate(
        _request("/v1internal:generateContent", _body("gemini-3.5-flash-low")),
    ))

    assert response.status_code == 401
    error = json.loads(response.body)["error"]
    assert error["code"] == 401
    assert error["status"] == "UNAUTHENTICATED"
    assert "gemini-3.5-flash-low" in error["message"]
    assert native_calls == []


def test_unmapped_live_envelope_with_google_credentials_uses_native_upstream(monkeypatch):
    native_calls = []

    async def native(request, is_stream, raw_body):
        native_calls.append((is_stream, raw_body))
        return Response(b"native")

    cs.replace_config(_config(enabled=True, mappings={}))
    monkeypatch.setattr(main, "_forward_antigravity_native", native)

    response = asyncio.run(main.antigravity_generate(_request(
        "/v1internal:streamGenerateContent",
        _body("gemini-3.5-flash-low"),
        {"authorization": "Bearer credential-present"},
    )))

    assert response.body == b"native"
    assert native_calls and native_calls[0][0] is True


def test_live_shaped_mapped_request_uses_outer_antigravity_source_slot_before_adapter_normalization(monkeypatch):
    captured = {}
    _block_forensics(monkeypatch)

    async def process(body, **kwargs):
        captured["body"] = body
        captured["kwargs"] = kwargs
        return Response(b"mapped")

    config = _config(mappings={"gemini-3.1-pro-low": "GLM-5.2"})
    config["combos"].append({"alias": "GLM-5.2", "chain": ["fake/fake-model"]})
    cs.replace_config(config)
    monkeypatch.setattr(main, "_process_chat_completion", process)
    response = asyncio.run(main.antigravity_generate(
        _request("/v1internal:generateContent", _body("gemini-3.1-pro-low")),
        model="gemini-3.5-flash-low",
    ))

    assert response.body == b"mapped"
    assert captured["body"]["model"] == "GLM-5.2"
    assert captured["kwargs"]["client_wants_gemini"] is True


def test_mitm_alias_branch_binds_all_locals_and_preserves_source_attribution(monkeypatch):
    """MITM-routed mapped request must not raise UnboundLocalError; source model is
    recovered from the trusted internal header, not the rewritten body.model."""
    captured = {}
    _block_forensics(monkeypatch)

    async def process(body, **kwargs):
        captured["body"] = body
        return Response(b"mapped")

    config = _config(mappings={"gemini-3.1-pro-low": "GLM-5.2"})
    config["combos"].append({"alias": "GLM-5.2", "chain": ["fake/fake-model"]})
    cs.replace_config(config)
    monkeypatch.setattr(main, "_process_chat_completion", process)

    # Simulate MITM: body.model rewritten to alias, alias + source-model headers set.
    response = asyncio.run(main.antigravity_generate(
        _request(
            "/v1internal:generateContent",
            _body("GLM-5.2"),
            {
                "x-bsl-antigravity-alias": "GLM-5.2",
                "x-bsl-antigravity-source-model": "gemini-3.1-pro-low",
            },
        ),
        model="gemini-3.5-flash-low",
    ))

    assert response.body == b"mapped"
    # mapping target reaches dispatch
    assert captured["body"]["model"] == "GLM-5.2"
    # original source attribution preserved (not the alias, not the path fallback)
    assert captured["body"]["_bsl_original_model"] == "gemini-3.1-pro-low"


def test_mitm_alias_branch_falls_back_to_payload_model_when_source_header_absent(monkeypatch):
    """If MITM sets the alias header but omits the source-model header, main falls
    back to the rewritten payload model as a bounded attribution source."""
    captured = {}
    _block_forensics(monkeypatch)

    async def process(body, **kwargs):
        captured["body"] = body
        return Response(b"mapped")

    config = _config(mappings={"gemini-3.1-pro-low": "GLM-5.2"})
    config["combos"].append({"alias": "GLM-5.2", "chain": ["fake/fake-model"]})
    cs.replace_config(config)
    monkeypatch.setattr(main, "_process_chat_completion", process)

    # MITM alias header present, but no source-model header; body.model is the alias.
    response = asyncio.run(main.antigravity_generate(
        _request(
            "/v1internal:generateContent",
            _body("GLM-5.2"),
            {"x-bsl-antigravity-alias": "GLM-5.2"},
        ),
        model="gemini-3.1-pro-low",
    ))

    assert response.body == b"mapped"
    assert captured["body"]["_bsl_original_model"] == "GLM-5.2"


def test_mitm_alias_malformed_body_never_falls_back_to_native(monkeypatch):
    """A committed MITM mapping remains BSL-only even when JSON parsing fails."""
    native_called = {"count": 0}
    _block_forensics(monkeypatch)

    async def native(*_args, **_kwargs):
        native_called["count"] += 1
        return Response(b"native")

    monkeypatch.setattr(main, "_forward_antigravity_native", native)
    response = asyncio.run(main.antigravity_generate(
        _request(
            "/v1internal:streamGenerateContent",
            {},
            {
                "x-bsl-antigravity-alias": "GLM-5.2",
                "x-bsl-antigravity-source-model": "gemini-3.1-pro-low",
            },
            raw_body=b"{malformed",
        ),
    ))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body_text = response.body.decode("utf-8", errors="replace")
    assert "finishReason" in body_text
    assert "STOP" in body_text
    assert body_text.rstrip().endswith("data: [DONE]")
    assert native_called["count"] == 0


def test_mitm_alias_mapped_failure_returns_terminal_sse_without_native_fallback(monkeypatch):
    """A mapped BSL slot that fails at dispatch must return a terminal SSE error.
    Native fallback must NOT be consulted for a committed MITM-mapped request."""
    native_called = {"count": 0}
    _block_forensics(monkeypatch)

    async def failed(*_args, **_kwargs):
        return JSONResponse({"error": "resolver unavailable"}, status_code=503)

    async def native(*_args, **_kwargs):
        native_called["count"] += 1
        return Response(b"native")

    cs.replace_config(_config(mappings={"gemini-3.5-flash-low": "coder-1"}))
    monkeypatch.setattr(main, "_process_chat_completion", failed)
    monkeypatch.setattr(main, "_forward_antigravity_native", native)

    response = asyncio.run(main.antigravity_generate(
        _request(
            "/v1internal:streamGenerateContent",
            _body("coder-1"),
            {
                "x-bsl-antigravity-alias": "coder-1",
                "x-bsl-antigravity-source-model": "gemini-3.5-flash-low",
            },
        ),
    ))

    # Terminal SSE error (status 200 event-stream with error payload + done frame).
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body_text = response.body.decode("utf-8", errors="replace")
    assert "resolver unavailable" in body_text or "BSL_ERROR" in body_text
    assert "[DONE]" in body_text
    # Native fallback was never consulted.
    assert native_called["count"] == 0


def test_mapped_resolver_failure_returns_bsl_error_without_native_fallback(monkeypatch):
    native_called = {"count": 0}
    _block_forensics(monkeypatch)

    async def failed(*_args, **_kwargs):
        return JSONResponse({"error": "resolver unavailable"}, status_code=503)

    async def native(*_args, **_kwargs):
        native_called["count"] += 1
        return Response(b"native")

    cs.replace_config(_config(mappings={"gemini-3.5-flash-low": "coder-1"}))
    monkeypatch.setattr(main, "_process_chat_completion", failed)
    monkeypatch.setattr(main, "_forward_antigravity_native", native)

    response = asyncio.run(main.antigravity_generate(_request(
        "/v1internal:generateContent", _body(), {"authorization": "Bearer test"},
    )))
    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": {"code": 503, "message": "resolver unavailable", "status": "BSL_ERROR"}
    }
    assert native_called["count"] == 0


def test_native_fallback_preserves_auth_safe_headers_and_rejects_local_recursion():
    request = _request(
        "/v1internal:generateContent",
        _body(),
        {
            "authorization": "Bearer secret",
            "x-goog-api-key": "google-secret",
            "x-client-data": "client-data",
            "cookie": "do-not-forward",
            "x-unrelated": "do-not-forward",
        },
    )

    headers = main._antigravity_native_request_headers(request)

    assert headers["authorization"] == "Bearer secret"
    assert headers["x-goog-api-key"] == "google-secret"
    assert headers["x-client-data"] == "client-data"
    assert "cookie" not in {name.lower() for name in headers}
    assert "x-unrelated" not in {name.lower() for name in headers}
    assert main._build_antigravity_native_url(request).startswith("https://daily-cloudcode-pa.googleapis.com/")
    with pytest.raises(ValueError, match="approved Google Cloud Code HTTPS origin"):
        main._validated_antigravity_native_base_url("http://127.0.0.1:6969")


def test_native_streaming_response_passes_bytes_and_closes_upstream(monkeypatch):
    stream = _BytesStream([b"data: native\n\n", b"data: [DONE]\n\n"])
    upstream = httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-goog-request-id": "test"},
        stream=stream,
    )
    client = _NativeClient(upstream)
    monkeypatch.setattr(main, "google_egress_client", client)
    request = _request("/v1internal:streamGenerateContent", _body(), {"authorization": "Bearer secret"})

    async def scenario():
        response = await main._forward_antigravity_native(request, True, b'{"request":"raw"}')
        chunks = [chunk async for chunk in response.body_iterator]
        await response.background()
        return response, chunks

    response, chunks = asyncio.run(scenario())

    assert chunks == [b"data: native\n\n", b"data: [DONE]\n\n"]
    assert response.headers["content-type"] == "text/event-stream"
    assert client.request.url.host == "daily-cloudcode-pa.googleapis.com"
    assert client.request.headers["authorization"] == "Bearer secret"
    assert stream.closed is True


def test_direct_integration_ui_renders_exact_antigravity_211_menu_without_removed_rows():
    source = open("app/static/app.js", encoding="utf-8").read()
    tab = source.split("const AG_SLOTS=", 1)[1].split("window.updateAlias =", 1)[0]
    rendered_slots = re.findall(r"\{key:'([^']+)',label:'([^']+)'\}", tab)

    assert rendered_slots == list(zip(EXPECTED_AG_SLOTS, EXPECTED_AG_LABELS))
    assert "Antigravity Integration" in tab
    assert "Antigravity IDE 2.1.1 model menu" in tab
    assert "Use native Antigravity model (unmapped)" in tab
    assert "new Antigravity conversations" in tab
    assert "Retry/Continue can retain the original conversation model" in tab
    assert "Unmapped direct-Overlay slots require Google credentials forwarded by Antigravity" in tab
    assert "otherwise map them to a BSL target" in tab
    assert 'data-ag-slot="${k}"' in tab
    assert 'data-ag-slot-count="${AG_SLOTS.length}"' in tab
    assert "/api/antigravity-integration/start" in tab
    assert not set(REMOVED_AG_SLOTS).intersection(key for key, _ in rendered_slots)
    assert "Gemini 3 Flash Agent" not in tab
    assert "Extra Low" not in tab
    assert "Pro Request" not in tab
    assert "Gemini Default" not in tab
    assert "Flash (Command)" not in tab
    assert "GitHub Copilot" not in tab
    assert "Kiro" not in tab
    assert "toggleIdeDns" not in tab
    assert "/api/mitm" not in tab


def test_mapping_save_failure_rolls_back_and_surfaces_error_toast():
    source = open("app/static/app.js", encoding="utf-8").read()
    handler = source.split("window.updateAntigravityIntegrationMapping=", 1)[1].split("window.toggleAntigravityIntegration", 1)[0]

    assert "Object.prototype.hasOwnProperty.call(c.mappings,k)" in handler
    assert "if(had)c.mappings[k]=previous;else delete c.mappings[k]" in handler
    assert "if(!await saveConfig())throw Error" in handler
    assert "showToast(`Failed to save Antigravity mapping" in handler
