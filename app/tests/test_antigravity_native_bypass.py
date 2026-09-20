"""
Antigravity /api/test-model native-OAuth bypass — golden tests (2026-09-20).

Pins the three-way dispatch contract in _test_antigravity_model:
  1. mapped + live target   -> dispatcher probe via _process_chat_completion
  2. mapped + dead target   -> mapping dropped; a slot in routable form
                               (combo alias / provider-model string) self-probes
                               through the dispatcher, any other slot falls
                               through to the native Google probe
  3. unmapped + unroutable -> native OAuth bypass (_test_antigravity_model_native),
                               validated Google origin + hosts-bypassing client

Also covers _test_antigravity_model_native's own egress mechanics (validated
daily-cloudcode-pa origin, fresh-token Authorization header, Gemini
usageMetadata parsing, error/exception surfacing) and the
_is_known_antigravity_mapping_target predicate guarding all of it.

Routable-target forms are exactly two: combo aliases and "provider/model"
strings. A bare model id registered under the antigravity provider is NOT a
BSL-routable target (the dispatcher cannot resolve it), so such slots are
native OAuth models and must take the native bypass.

Pure offline: every network seam is monkeypatched. No live calls.

Run: .venv\\Scripts\\python -m pytest app/tests/test_antigravity_native_bypass.py -q
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import app.main as main
import app.oauth as oauth_module


# ─── fixtures ────────────────────────────────────────────────────────────────

def _base_config():
    """Minimal config: two providers + two combo aliases.

    NOTE: the predicate recognizes only combo aliases and provider/model
    strings. `gemini-2.5-pro` is registered under the antigravity provider
    but is a BARE id — not a routable target — so for dispatch purposes it is
    a native OAuth slot exactly like the unregistered `gemini-4-nano-preview`.
    """
    return {
        "providers": {
            "antigravity": {
                "enabled": True,
                "models": [{"id": "gemini-3.1-pro-preview"}, {"id": "gemini-2.5-pro"}],
            },
            "vsllm": {
                "enabled": True,
                "models": [{"id": "GLM-5.2"}],
            },
        },
        "combos": [{"alias": "coder-3"}, {"alias": "gemini-pro-chain"}],
    }


def _patch_obs(monkeypatch):
    """Keep the observability store out of offline tests."""
    monkeypatch.setattr(main.obs, "log_request", lambda *a, **k: None)
    monkeypatch.setattr(main.obs, "log_request_start", lambda *a, **k: "req-test")


def _patch_env(monkeypatch, mappings):
    """Patch every non-pure seam _test_antigravity_model touches."""
    cfg = _base_config()
    _patch_obs(monkeypatch)
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    monkeypatch.setattr(
        main, "resolve_active_connection",
        lambda config, provider, model: ({"id": "c1"}, "active"),
    )

    async def _fresh_token(provider, conn, prov):
        return "tok123"

    monkeypatch.setattr(oauth_module, "ensure_fresh_token", _fresh_token)
    monkeypatch.setattr(
        main, "_antigravity_integration_settings", lambda: {"mappings": dict(mappings)}
    )
    # Exact-match mappings in every test; neutralize the fuzzy resolver.
    monkeypatch.setattr(main, "maybe_fuzzy_normalize_model", lambda model, cfg2: (model, None))
    return cfg


class _DispatchRecorder:
    """Records which phase-2 executor fired, with what payload."""

    def __init__(self):
        self.native_calls = []
        self.dispatcher_calls = []

    def install(self, monkeypatch):
        async def _native(provider, model, token, config, request_id, t0):
            self.native_calls.append((provider, model, token))
            return main.JSONResponse({"ok": True, "route": "native", "native_model": model})

        async def _dispatch(body):
            self.dispatcher_calls.append(dict(body))
            return main.JSONResponse(
                {
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                    "choices": [{"message": {"content": "OK"}}],
                },
                status_code=200,
            )

        monkeypatch.setattr(main, "_test_antigravity_model_native", _native)
        monkeypatch.setattr(main, "_process_chat_completion", _dispatch)
        return self


class _FakeUpstreamResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    async def aclose(self):
        pass


class _FakeEgressClient:
    """Stands in for the hosts-bypassing Google egress client."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.built = []

    def build_request(self, method, url, headers=None, content=None):
        req = {"method": method, "url": url, "headers": dict(headers or {}), "content": content}
        self.built.append(req)
        return req

    async def send(self, request):
        if self.exc:
            raise self.exc
        return self.response


# ─── predicate: _is_known_antigravity_mapping_target ─────────────────────────

def test_mapping_target_predicate_accepts_combo_alias_and_provider_model():
    cfg = _base_config()
    assert main._is_known_antigravity_mapping_target(cfg, "coder-3") is True          # combo alias
    assert main._is_known_antigravity_mapping_target(cfg, "gemini-pro-chain") is True
    assert main._is_known_antigravity_mapping_target(cfg, "vsllm/GLM-5.2") is True     # provider/model


def test_mapping_target_predicate_rejects_unknown_malformed_and_bare_ids():
    cfg = _base_config()
    assert main._is_known_antigravity_mapping_target(cfg, "vsllm/missing") is False
    assert main._is_known_antigravity_mapping_target(cfg, "ghost-combo") is False
    assert main._is_known_antigravity_mapping_target(cfg, "gemini-2.5-pro") is False    # bare id: registered, still unroutable
    assert main._is_known_antigravity_mapping_target(cfg, "noslash") is False           # no separator
    assert main._is_known_antigravity_mapping_target(cfg, "/leading") is False          # empty provider
    assert main._is_known_antigravity_mapping_target(cfg, "trailing/") is False         # empty model


# ─── dispatch: _test_antigravity_model ───────────────────────────────────────

def test_unmapped_unregistered_slot_bypasses_dispatcher(monkeypatch):
    """A model mapped nowhere and routable nowhere is a native OAuth slot:
    it must never enter the alias-mapping dispatcher."""
    _patch_env(monkeypatch, mappings={})
    rec = _DispatchRecorder().install(monkeypatch)

    resp = asyncio.run(main._test_antigravity_model("antigravity", "gemini-4-nano-preview"))

    body = json.loads(resp.body)
    assert rec.dispatcher_calls == []
    assert rec.native_calls == [("antigravity", "gemini-4-nano-preview", "tok123")]
    assert body["route"] == "native"


def test_live_mapping_routes_through_dispatcher(monkeypatch):
    """Exact mapping whose target is a live combo alias: probe the full
    Gemini->OpenAI->upstream chain with the mapping's alias."""
    _patch_env(monkeypatch, mappings={"gemini-2.5-pro": "coder-3"})
    rec = _DispatchRecorder().install(monkeypatch)

    resp = asyncio.run(main._test_antigravity_model("antigravity", "gemini-2.5-pro"))

    body = json.loads(resp.body)
    assert rec.native_calls == []
    assert len(rec.dispatcher_calls) == 1
    assert rec.dispatcher_calls[0]["model"] == "coder-3"
    assert body["ok"] is True
    assert body["combo_alias"] == "coder-3"
    assert body["in_tokens"] == 3
    assert body["out_tokens"] == 4
    assert body["reply"] == "OK"


def test_dead_mapping_on_bare_id_falls_to_native(monkeypatch):
    """Dead mapping target (combo removed after save) on a bare-id slot: drop
    the mapping, and the bare id still is not a routable target, so the
    native bypass applies (drop-then-bypass chain). Registration under the
    antigravity provider does NOT make a bare id dispatcher-routable."""
    _patch_env(monkeypatch, mappings={"gemini-2.5-pro": "ghost/combo-x"})
    rec = _DispatchRecorder().install(monkeypatch)

    resp = asyncio.run(main._test_antigravity_model("antigravity", "gemini-2.5-pro"))

    body = json.loads(resp.body)
    assert rec.dispatcher_calls == []
    assert rec.native_calls == [("antigravity", "gemini-2.5-pro", "tok123")]
    assert body["route"] == "native"


def test_dead_mapping_on_unregistered_slot_falls_to_native(monkeypatch):
    """Dead mapping on an unregistered bare-id slot: same drop-then-bypass
    chain as the registered case."""
    _patch_env(monkeypatch, mappings={"gemini-4-nano-preview": "ghost/combo-x"})
    rec = _DispatchRecorder().install(monkeypatch)

    resp = asyncio.run(main._test_antigravity_model("antigravity", "gemini-4-nano-preview"))

    body = json.loads(resp.body)
    assert rec.dispatcher_calls == []
    assert rec.native_calls == [("antigravity", "gemini-4-nano-preview", "tok123")]
    assert body["route"] == "native"


def test_dead_mapping_on_provider_model_form_self_probes(monkeypatch):
    """Dead mapping target on a slot already in provider/model form: drop the
    dead mapping, then self-probe the model through the dispatcher instead
    of surfacing an unrelated upstream 400 (drop-then-self-probe chain)."""
    _patch_env(monkeypatch, mappings={"vsllm/GLM-5.2": "ghost/combo-x"})
    rec = _DispatchRecorder().install(monkeypatch)

    resp = asyncio.run(main._test_antigravity_model("antigravity", "vsllm/GLM-5.2"))

    body = json.loads(resp.body)
    assert rec.native_calls == []
    assert len(rec.dispatcher_calls) == 1
    assert rec.dispatcher_calls[0]["model"] == "vsllm/GLM-5.2"  # self-probe, not the dead alias
    assert body["ok"] is True
    assert body["combo_alias"] == "vsllm/GLM-5.2"


def test_unmapped_provider_model_form_self_probes(monkeypatch):
    """A model already in provider/model form is itself a routable target:
    dispatcher probe uses it verbatim (replaces the old first-mapping
    fallback that misrouted to an unrelated combo)."""
    _patch_env(monkeypatch, mappings={})
    rec = _DispatchRecorder().install(monkeypatch)

    resp = asyncio.run(main._test_antigravity_model("antigravity", "vsllm/GLM-5.2"))

    body = json.loads(resp.body)
    assert rec.native_calls == []
    assert len(rec.dispatcher_calls) == 1
    assert rec.dispatcher_calls[0]["model"] == "vsllm/GLM-5.2"


# ─── native probe: _test_antigravity_model_native ────────────────────────────

_GEMINI_OK = json.dumps({
    "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
}).encode("utf-8")


def test_native_success_parses_gemini_usage_and_targets_google_origin(monkeypatch):
    _patch_obs(monkeypatch)
    client = _FakeEgressClient(_FakeUpstreamResponse(200, _GEMINI_OK))
    monkeypatch.setattr(main, "_get_antigravity_egress_client", lambda: client)

    resp = asyncio.run(main._test_antigravity_model_native(
        "antigravity", "gemini-3.1-pro-preview", "tok123", _base_config(), "req-1", time.time()
    ))

    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["route"] == "native"
    assert body["in_tokens"] == 5
    assert body["out_tokens"] == 2
    assert body["total_tokens"] == 7
    assert body["reply"] == "OK"

    assert len(client.built) == 1
    req = client.built[0]
    assert req["url"] == (
        "https://daily-cloudcode-pa.googleapis.com"
        "/v1beta/models/gemini-3.1-pro-preview:generateContent"
    )
    assert req["headers"]["Authorization"] == "Bearer tok123"
    assert req["headers"]["Content-Type"] == "application/json"


def test_native_upstream_error_surfaces_status(monkeypatch):
    _patch_obs(monkeypatch)
    client = _FakeEgressClient(_FakeUpstreamResponse(429, b'{"error":"quota exceeded"}'))
    monkeypatch.setattr(main, "_get_antigravity_egress_client", lambda: client)

    resp = asyncio.run(main._test_antigravity_model_native(
        "antigravity", "gemini-3.1-pro-preview", "tok123", _base_config(), "req-1", time.time()
    ))

    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["ok"] is False
    assert "quota" in body["error"]


def test_native_transport_failure_returns_502(monkeypatch):
    _patch_obs(monkeypatch)
    client = _FakeEgressClient(exc=RuntimeError("boom"))
    monkeypatch.setattr(main, "_get_antigravity_egress_client", lambda: client)

    resp = asyncio.run(main._test_antigravity_model_native(
        "antigravity", "gemini-3.1-pro-preview", "tok123", _base_config(), "req-1", time.time()
    ))

    body = json.loads(resp.body)
    assert resp.status_code == 502
    assert body["ok"] is False
    assert body["error"] == "Native Google probe failed: RuntimeError"


def test_native_rejects_invalid_model_id_without_egress(monkeypatch):
    _patch_obs(monkeypatch)
    client = _FakeEgressClient()
    monkeypatch.setattr(main, "_get_antigravity_egress_client", lambda: client)

    resp = asyncio.run(main._test_antigravity_model_native(
        "antigravity", "bad model!", "tok123", _base_config(), "req-1", time.time()
    ))

    body = json.loads(resp.body)
    assert resp.status_code == 400
    assert body["ok"] is False
    assert "Invalid native model id" in body["error"]
    assert client.built == []  # rejected before any upstream dial
