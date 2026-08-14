"""Tests for Vision Scout response parsing and fallback behavior.

Covers:
  - the original 'Expecting value: line 1 column 1 (char 0)' fix, caused by
    providers returning SSE or empty bodies when stream=False
  - the 2026-08-03 deadlock fix: combo aliases resolve in-process to direct
    provider candidates (never a BSL Router self-call) and are walked with
    per-leaf fallback
"""
import asyncio
import json
import sys
sys.path.insert(0, ".")

import httpx
from unittest.mock import AsyncMock, MagicMock
from app.models import ChatCompletionRequest
from app.scouts.vision import (
    _describe_image,
    _resolve_vision_candidates,
    clear_vision_cache,
    polyfill_vision,
    PLACEHOLDER_UNREADABLE,
)


def _make_candidates(*specs):
    """Build a candidate list: (conn, model, provider) tuples."""
    if not specs:
        specs = (("https://api.example.com", "kimi-k3", "example"),)
    return [
        ({"base_url": base_url, "api_key": "sk-test"}, model, provider)
        for base_url, model, provider in specs
    ]


def _make_image_url(url="data:image/png;base64,iVBOR"):
    return {"url": url, "detail": "high"}


def _mock_client(*responses):
    """Mock client whose .post returns/raises each item in sequence."""
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=list(responses))
    return mock_client


def _mock_resp(text, status_code=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def _run(image_url, client, candidates, max_tokens=1024, ui_ux=False):
    return asyncio.run(
        _describe_image(image_url, client, candidates, max_tokens, ui_ux)
    )


def test_sse_response_parsed_correctly():
    """Provider returns SSE despite stream=False - should parse deltas."""
    clear_vision_cache()
    sse_body = "\n".join([
        'data: {"choices":[{"delta":{"content":"A screenshot of "}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"a login page."}}]}',
        "",
        'data: [DONE]',
        "",
    ])
    result = _run(_make_image_url(), _mock_client(_mock_resp(sse_body)), _make_candidates())
    assert "login page" in result, f"Expected 'login page' in result, got: {result!r}"
    print("SSE response test PASS")


def test_empty_response_body_handled():
    """Provider returns 200 with empty body - candidate yields no content."""
    clear_vision_cache()
    result = _run(_make_image_url(), _mock_client(_mock_resp("")), _make_candidates())
    assert result is None, f"Expected None on exhaustion, got: {result!r}"
    print("Empty body test PASS")


def test_non_json_response_handled():
    """Provider returns non-JSON text - candidate yields no content."""
    clear_vision_cache()
    result = _run(
        _make_image_url(),
        _mock_client(_mock_resp("<html>Bad Gateway</html>")),
        _make_candidates(),
    )
    assert result is None, f"Expected None on exhaustion, got: {result!r}"
    print("Non-JSON test PASS")


def test_standard_json_response_still_works():
    """Standard JSON response - should extract content normally."""
    clear_vision_cache()
    json_body = json.dumps({
        "choices": [{"message": {"content": "A blue dashboard with charts."}}]
    })
    result = _run(_make_image_url(), _mock_client(_mock_resp(json_body)), _make_candidates())
    assert "blue dashboard" in result, f"Expected 'blue dashboard' in result, got: {result!r}"
    print("Standard JSON test PASS")


def test_http_error_advances_to_next_candidate():
    """A 404 on the first leaf must fall back to the second, not stall."""
    clear_vision_cache()
    dead = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404)
    )
    good = _mock_resp(json.dumps({
        "choices": [{"message": {"content": "Described by the backup model."}}]
    }))
    client = _mock_client(dead, good)
    candidates = _make_candidates(
        ("https://dead.example.com", "gemini-3.5-flash-high", "antigravity"),
        ("https://live.example.com", "kimi-k3-free", "tokenrouter"),
    )
    result = _run(_make_image_url(), client, candidates)
    assert "backup model" in result, f"Expected fallback to succeed, got: {result!r}"
    assert client.post.await_count == 2, "Expected exactly 2 upstream attempts"
    print("HTTP error fallback test PASS")


def test_all_candidates_failing_returns_none_not_hang():
    """Every leaf dead - must return None promptly, never hang."""
    clear_vision_cache()
    err = httpx.ConnectError("connection refused")
    client = _mock_client(err, err)
    candidates = _make_candidates(
        ("https://a.example.com", "model-a", "prov-a"),
        ("https://b.example.com", "model-b", "prov-b"),
    )
    result = _run(_make_image_url(), client, candidates)
    assert result is None, f"Expected None on exhaustion, got: {result!r}"
    assert client.post.await_count == 2, "Expected both candidates to be tried"
    print("All-candidates-fail test PASS")


def test_cache_hit_skips_all_upstream_calls():
    """Second describe of the same image must not touch the network."""
    clear_vision_cache()
    body = json.dumps({"choices": [{"message": {"content": "Cached description."}}]})
    client = _mock_client(_mock_resp(body))
    candidates = _make_candidates()

    first = _run(_make_image_url(), client, candidates)
    assert "Cached description" in first
    assert client.post.await_count == 1

    second = _run(_make_image_url(), client, candidates)
    assert second == first
    assert client.post.await_count == 1, "Cache hit must not issue another request"
    print("Cache hit test PASS")


def test_combo_resolution_never_returns_self_reference():
    """
    Regression guard for the 2026-08-03 server-wide freeze.

    Combo aliases must expand to direct provider connections. A candidate
    pointing back at BSL Router's own port would re-enter the routing
    pipeline and deadlock the event loop.
    """
    clear_vision_cache()
    config = {
        "server": {"host": "0.0.0.0", "port": 6969},
        "combos": [{
            "alias": "Vision",
            "chain": [
                {"provider": "antigravity", "model": "gemini-3.5-flash-high"},
                "tokenrouter/kimi-k3-free",
            ],
            "strategy": "fallback",
        }],
        "providers": {
            "antigravity": {
                "models": [{"id": "gemini-3.5-flash-high", "enabled": True}],
                "connections": [{"enabled": True, "base_url": "https://ag.example.com", "api_key": "k"}],
            },
            "tokenrouter": {
                "models": [{"id": "kimi-k3-free", "enabled": True}],
                "connections": [{"enabled": True, "base_url": "https://tr.example.com", "api_key": "k"}],
            },
        },
    }
    candidates = _resolve_vision_candidates(config, "Vision")
    assert len(candidates) == 2, f"Expected 2 candidates, got {len(candidates)}"
    for conn, model, provider in candidates:
        base_url = conn.get("base_url", "")
        assert "127.0.0.1" not in base_url, f"Self-reference leaked: {base_url}"
        assert "localhost" not in base_url, f"Self-reference leaked: {base_url}"
        assert ":6969" not in base_url, f"Self-reference leaked: {base_url}"
    assert candidates[0][1] == "gemini-3.5-flash-high"
    assert candidates[1][1] == "kimi-k3-free"
    print("No-self-reference test PASS")


def test_disabled_and_unreachable_leaves_are_skipped():
    """Leaves that are disabled or lack a base_url must not become candidates."""
    clear_vision_cache()
    config = {
        "combos": [{
            "alias": "Vision",
            "chain": [
                {"provider": "p1", "model": "disabled-model"},
                {"provider": "p2", "model": "no-url-model"},
                {"provider": "p3", "model": "good-model"},
            ],
        }],
        "providers": {
            "p1": {
                "models": [{"id": "disabled-model", "enabled": False}],
                "connections": [{"enabled": True, "base_url": "https://p1.example.com"}],
            },
            "p2": {
                "models": [{"id": "no-url-model", "enabled": True}],
                "connections": [{"enabled": True, "base_url": ""}],
            },
            "p3": {
                "models": [{"id": "good-model", "enabled": True}],
                "connections": [{"enabled": True, "base_url": "https://p3.example.com"}],
            },
        },
    }
    candidates = _resolve_vision_candidates(config, "Vision")
    assert len(candidates) == 1, f"Expected only the good leaf, got {candidates}"
    assert candidates[0][1] == "good-model"
    print("Skip-unusable-leaves test PASS")


def test_nested_combo_alias_expands():
    """A chain entry that is itself a combo alias must expand transitively."""
    clear_vision_cache()
    config = {
        "combos": [
            {"alias": "Vision", "chain": ["Inner", {"provider": "p2", "model": "m2"}]},
            {"alias": "Inner", "chain": [{"provider": "p1", "model": "m1"}]},
        ],
        "providers": {
            "p1": {
                "models": [{"id": "m1", "enabled": True}],
                "connections": [{"enabled": True, "base_url": "https://p1.example.com"}],
            },
            "p2": {
                "models": [{"id": "m2", "enabled": True}],
                "connections": [{"enabled": True, "base_url": "https://p2.example.com"}],
            },
        },
    }
    candidates = _resolve_vision_candidates(config, "Vision")
    assert [c[1] for c in candidates] == ["m1", "m2"], f"Got {candidates}"
    print("Nested combo test PASS")


def test_cyclic_combo_does_not_infinite_loop():
    """Self-referencing combos must terminate via the depth guard."""
    clear_vision_cache()
    config = {
        "combos": [
            {"alias": "A", "chain": ["B"]},
            {"alias": "B", "chain": ["A"]},
        ],
        "providers": {},
    }
    candidates = _resolve_vision_candidates(config, "A")
    assert candidates == [], f"Expected no candidates from cyclic combo, got {candidates}"
    print("Cyclic combo test PASS")


# ─── Sequencing contract: vision must yield output before the model runs ────

_PIPE_CONFIG = {
    "tools": {"vision_bridge_enabled": True, "vision_bridge_model": "Vision"},
    "combos": [{"alias": "Vision", "chain": [{"provider": "vp", "model": "vm"}]}],
    "providers": {
        "vp": {
            "models": [{"id": "vm", "enabled": True}],
            "connections": [{"enabled": True, "base_url": "https://v.example.com", "api_key": "k"}],
        },
    },
}


def _image_request(n_images=1):
    parts = [{"type": "text", "text": "What is this?"}]
    for i in range(n_images):
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,IMG{i}"}})
    return ChatCompletionRequest(
        model="text-only-model",
        messages=[{"role": "user", "content": parts}],
    )


def _ok_resp(text="A red square."):
    return _mock_resp(json.dumps({"choices": [{"message": {"content": text}}]}))


def test_total_vision_failure_aborts_before_target_model():
    """
    Fail-open contract: if the Scout produced no sight at all, the request
    still continues with a placeholder. The target model gets a text saying
    the image couldn't be read, which is better than a hard 502 that blocks
    all responses.
    """
    clear_vision_cache()
    err = httpx.ConnectError("refused")
    client = _mock_client(err)
    out = asyncio.run(polyfill_vision(_image_request(), client, _PIPE_CONFIG))

    blob = json.dumps([p if isinstance(p, dict) else p.model_dump()
                       for p in out.messages[0].content])
    assert PLACEHOLDER_UNREADABLE in blob, f"Placeholder missing on fail-open: {blob}"
    print("Total-failure fail-open test PASS")


def test_partial_vision_failure_still_proceeds():
    """One image readable, one not - the request is still worth serving."""
    clear_vision_cache()
    client = _mock_client(_ok_resp("A red square."), httpx.ConnectError("refused"))
    out = asyncio.run(polyfill_vision(_image_request(2), client, _PIPE_CONFIG))

    blob = json.dumps([p if isinstance(p, dict) else p.model_dump()
                       for p in out.messages[0].content])
    assert "red square" in blob, f"Successful description missing: {blob}"
    assert PLACEHOLDER_UNREADABLE in blob, f"Placeholder missing for failed image: {blob}"
    print("Partial-failure test PASS")


def test_successful_vision_replaces_image_with_text():
    """Happy path: image parts are fully substituted by text descriptions."""
    clear_vision_cache()
    client = _mock_client(_ok_resp("A blue login form."))
    out = asyncio.run(polyfill_vision(_image_request(), client, _PIPE_CONFIG))

    parts = out.messages[0].content
    kinds = [p.get("type") if isinstance(p, dict) else p.type for p in parts]
    assert "image_url" not in kinds, f"Image part survived polyfill: {kinds}"
    blob = json.dumps([p if isinstance(p, dict) else p.model_dump() for p in parts])
    assert "blue login form" in blob, f"Description not injected: {blob}"
    print("Happy-path substitution test PASS")


def test_no_images_skips_scout_entirely():
    """Text-only request must not trigger any vision upstream call."""
    clear_vision_cache()
    client = _mock_client()
    req = ChatCompletionRequest(
        model="text-only-model",
        messages=[{"role": "user", "content": "just text"}],
    )
    out = asyncio.run(polyfill_vision(req, client, _PIPE_CONFIG))
    assert out is req
    assert client.post.await_count == 0, "Scout dialled upstream for a text-only request"
    print("No-images skip test PASS")


def test_vision_capable_target_still_runs_scout():
    """The bridge describes images for EVERY target model.

    The VISION_CAPABLE_PATTERNS gate was deliberately removed: when the vision
    bridge is on it reads every image regardless of whether the destination
    model is natively multimodal. Upstream providers frequently fail to fetch
    remote image URLs themselves (403s from CDNs), so routing every image
    through the Scout is both simpler and more reliable.
    """
    clear_vision_cache()
    # The Scout WILL run for this model, so it needs a response to consume.
    client = _mock_client(_ok_resp("A wide dashboard screenshot."))
    req = _image_request()
    req.model = "gemini-3.1-pro"
    out = asyncio.run(polyfill_vision(req, client, _PIPE_CONFIG))
    assert client.post.await_count > 0, "Scout skipped a vision-capable model"
    # The image part must be replaced by the Scout's description.
    parts = out.messages[0].content
    kinds = [p.get("type") if isinstance(p, dict) else p.type for p in parts]
    assert "image_url" not in kinds, f"Image part survived polyfill: {kinds}"
    blob = json.dumps([p if isinstance(p, dict) else p.model_dump() for p in parts])
    assert "dashboard screenshot" in blob, f"Description not injected: {blob}"
    print("Vision-capable always-polyfill test PASS")


if __name__ == "__main__":
    test_sse_response_parsed_correctly()
    test_empty_response_body_handled()
    test_non_json_response_handled()
    test_standard_json_response_still_works()
    test_http_error_advances_to_next_candidate()
    test_all_candidates_failing_returns_none_not_hang()
    test_cache_hit_skips_all_upstream_calls()
    test_combo_resolution_never_returns_self_reference()
    test_disabled_and_unreachable_leaves_are_skipped()
    test_nested_combo_alias_expands()
    test_cyclic_combo_does_not_infinite_loop()
    test_total_vision_failure_aborts_before_target_model()
    test_partial_vision_failure_still_proceeds()
    test_successful_vision_replaces_image_with_text()
    test_no_images_skips_scout_entirely()
    test_vision_capable_target_still_runs_scout()
    print("\nAll Vision Scout tests PASS")
