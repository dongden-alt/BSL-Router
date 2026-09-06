"""H0 hot-fix: malformed request bodies must return clean JSON 400s.

Regression coverage for the ``_safe_request_json`` helper and the app-level
``_BSLInvalidRequestBody`` exception handler. Before the fix, a body such as
``b"{invalid"`` escaped ``await request.json()`` on every inference route as a
plain-text 500 traceback (json.JSONDecodeError) that clients like Claude Code
cannot parse. After the fix the same request yields a machine-readable
OpenAI-style JSON 400 on every inference path:

    /v1/chat/completions, /v1/messages, /v1/responses,
    /v1/images/generations, /v1/videos/generations
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main


INFERENCE_ROUTES = [
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
    "/v1/images/generations",
    "/v1/videos/generations",
]


def _request_with_body(body: bytes) -> Request:
    """Build a Starlette Request whose inbound stream yields ``body``."""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    return Request(scope, receive)


# ── Unit: _safe_request_json contract ────────────────────────────────────────


def test_safe_request_json_returns_parsed_body():
    request = _request_with_body(json.dumps({"model": "x"}).encode("utf-8"))
    assert asyncio.run(main._safe_request_json(request)) == {"model": "x"}


def test_safe_request_json_raises_typed_error_on_malformed_json():
    request = _request_with_body(b"{invalid")
    with pytest.raises(main._BSLInvalidRequestBody) as excinfo:
        asyncio.run(main._safe_request_json(request))
    assert excinfo.value.detail  # carries a client-facing .detail message


def test_safe_request_json_raises_on_empty_body():
    request = _request_with_body(b"")
    with pytest.raises(main._BSLInvalidRequestBody):
        asyncio.run(main._safe_request_json(request))


def test_safe_request_json_raises_on_invalid_utf8():
    request = _request_with_body(b'{"model": "\xff\xfe"}')
    with pytest.raises(main._BSLInvalidRequestBody):
        asyncio.run(main._safe_request_json(request))


# ── Integration: inference routes answer malformed bodies with JSON 400 ──────


@pytest.mark.parametrize("route", INFERENCE_ROUTES)
def test_inference_route_malformed_body_returns_json_400(route):
    client = TestClient(main.app)
    response = client.post(
        route,
        content=b"{invalid",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    payload = response.json()  # machine-parseable JSON, not a traceback
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["code"] == "invalid_json_body"
    assert "not valid JSON" in payload["error"]["message"]


@pytest.mark.parametrize("route", INFERENCE_ROUTES)
def test_inference_route_empty_body_returns_json_400(route):
    client = TestClient(main.app)
    response = client.post(route, content=b"", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json_body"


def test_exception_handler_is_registered_on_app():
    from starlette.routing import Match

    handler = main.app.exception_handlers.get(main._BSLInvalidRequestBody)
    assert callable(handler)
    assert main._BSLInvalidRequestBody is not None
    assert hasattr(main._BSLInvalidRequestBody("probe"), "detail")
    # Scope sanity: every route under test exists on the app and accepts POST.
    app_routes = [r for r in main.app.routes if hasattr(r, "matches")]
    for path in INFERENCE_ROUTES:
        assert any(
            app_route.matches({"type": "http", "method": "POST", "path": path}) != Match.NONE
            for app_route in app_routes
        ), path
