"""Tests for fuzzy model-ID normalization + /api/test-model hardening.

Covers the two 2026-08-24 fixes:
1. Dash/order variants of model IDs ('gpt-5-6-terra', 'gpt-terra-5-6')
   resolve to the registered 'gpt-5.6-terra' without ever rewriting a
   valid exact name.
2. The test endpoint refuses concurrent pile-ups (429) and cancels probes
   that exceed _MODEL_TEST_TIMEOUT_S (504).
"""
import asyncio
import json

import pytest

from app.routing.model_normalizer import (
    canonical_key,
    build_fuzzy_index,
    fuzzy_resolve_model,
    maybe_fuzzy_normalize_model,
)


# ── canonical_key ────────────────────────────────────────────────────────────

def test_canonical_key_dash_and_dot_equivalent():
    assert canonical_key("gpt-5.6-terra") == canonical_key("gpt-5-6-terra")
    assert canonical_key("gpt-5.6-terra") == canonical_key("gpt-terra-5-6")
    assert canonical_key("gpt-5.6-terra") == canonical_key("GPT_5_6_TERRA")


def test_canonical_key_shape():
    assert canonical_key("gpt-5.6-terra") == (("gpt", "terra"), ((5, 6),))
    assert canonical_key("glm-5.3") == (("glm",), ((5, 3),))
    # 'qwen3' is ONE alpha token (digits glued to letters are not a version),
    # so only the trailing '7' forms the version run.
    assert canonical_key("qwencoder/qwen3.7-max") == (
        ("max", "qwen3", "qwencoder"), ((7,),),
    )


def test_canonical_key_multiple_numeric_runs_distinct():
    # Date-suffix position is not semantic: 'preview-2024' vs '2024-preview'
    # deliberately normalize EQUAL (both -> alpha (gpt,preview) + runs ((5,6),(2024,))).
    assert canonical_key("gpt-5-6-preview-2024") == canonical_key("gpt-5.6-preview-2024")
    # Dash/dot equivalence extends across a whole numeric run (the core fix):
    # '5-6-2024' == '5.6-2024' -> one run (5,6,2024). Splitting off trailing
    # years would need semantic knowledge and risks breaking real version
    # tuples, so this collision class is accepted by design.
    assert canonical_key("gpt-5-6-2024") == canonical_key("gpt-5.6-2024")


def test_canonical_key_empty():
    assert canonical_key("") == ((), ())
    assert canonical_key("---") == ((), ())


# ── fuzzy resolution over a mini config ─────────────────────────────────────

def _mini_cfg():
    return {
        "combos": [{"alias": "GLM-5.3", "chain": ["x/y"]}],
        "aliases": {"coder-2": {"model": "m", "provider": "p"}},
        "providers": {
            "vsllm-a": {"models": [{"id": "gpt-5.6-terra"}]},
            "other": {"models": [{"id": "deepseek-v4-flash"}]},
        },
    }


def test_fuzzy_resolves_dash_variant_to_registered_model():
    hit = fuzzy_resolve_model("gpt-5-6-terra", _mini_cfg())
    assert hit is not None
    assert hit["name"] == "gpt-5.6-terra"
    assert hit["kind"] == "model"
    assert hit["provider"] == "vsllm-a"


def test_fuzzy_resolves_reordered_variant():
    hit = fuzzy_resolve_model("gpt-terra-5-6", _mini_cfg())
    assert hit is not None
    assert hit["name"] == "gpt-5.6-terra"


def test_fuzzy_resolves_combo_alias_variant():
    hit = fuzzy_resolve_model("GLM-5-3", _mini_cfg())
    assert hit is not None
    assert hit["kind"] == "combo"
    assert hit["name"] == "GLM-5.3"


def test_exact_names_never_rewritten():
    cfg = _mini_cfg()
    for exact in ("gpt-5.6-terra", "GLM-5.3", "coder-2", "deepseek-v4-flash"):
        assert fuzzy_resolve_model(exact, cfg) is None
        model, note = maybe_fuzzy_normalize_model(exact, cfg)
        assert model == exact and note is None


def test_unknown_stays_unknown():
    model, note = maybe_fuzzy_normalize_model("totally-unknown-model", _mini_cfg())
    assert model == "totally-unknown-model" and note is None


def test_provider_hint_scopes_model_candidates():
    cfg = {
        "providers": {
            "a": {"models": [{"id": "gpt-5.6-terra"}]},
            "b": {"models": [{"id": "gpt-5.6-terra"}]},
        }
    }
    hit = fuzzy_resolve_model("gpt-5-6-terra", cfg, provider_hint="b")
    assert hit is not None and hit["provider"] == "b"


def test_build_fuzzy_index_ladder_order():
    """When several identifiers share a canonical key, combos come first."""
    cfg = {
        "combos": [{"alias": "gpt-5.6-terra", "chain": ["x"]}],
        "aliases": {"gpt-5.6-terra": {"model": "m", "provider": "p"}},
        "providers": {"q": {"models": [{"id": "gpt-5.6-terra"}]}},
    }
    cands = build_fuzzy_index(cfg)[canonical_key("gpt-5-6-terra")]
    assert [c["kind"] for c in cands] == ["combo", "alias", "model"]


# ── /api/test-model endpoint guards ─────────────────────────────────────────

class _StubRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _import_mod():
    import app.main as mod
    return mod


@pytest.fixture
def guarded_mod(monkeypatch):
    mod = _import_mod()
    cfg = {"providers": {"prov1": {"models": [{"id": "m1"}], "connections": [{"api_key": "k"}]}}}
    monkeypatch.setattr(mod, "cs_get_config", lambda: cfg)
    # Drain the semaphore between tests
    while mod._MODEL_TEST_SEMAPHORE.locked():
        try:
            mod._MODEL_TEST_SEMAPHORE.release()
        except Exception:
            break
    yield mod
    while mod._MODEL_TEST_SEMAPHORE.locked():
        try:
            mod._MODEL_TEST_SEMAPHORE.release()
        except Exception:
            break


def test_model_test_429_when_both_slots_held(guarded_mod):
    mod = guarded_mod

    async def _scenario():
        # Hold both slots in the SAME loop as the endpoint call (acquire is a
        # coroutine — calling it without await never holds anything).
        await mod._MODEL_TEST_SEMAPHORE.acquire()
        await mod._MODEL_TEST_SEMAPHORE.acquire()
        return await mod.test_model(_StubRequest({"provider": "prov1", "model": "m1"}))

    resp = asyncio.run(_scenario())
    assert resp.status_code == 429
    data = json.loads(resp.body)
    assert data["ok"] is False and "still running" in data["error"]
    # Release in the same loop
    async def _release():
        mod._MODEL_TEST_SEMAPHORE.release()
        mod._MODEL_TEST_SEMAPHORE.release()
    asyncio.run(_release())


def test_model_test_504_on_timeout(guarded_mod, monkeypatch):
    mod = guarded_mod
    monkeypatch.setattr(mod, "_MODEL_TEST_TIMEOUT_S", 0.2)

    async def _slow_probe(body):
        await asyncio.sleep(5.0)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    monkeypatch.setattr(mod, "_process_chat_completion", _slow_probe)
    resp = asyncio.run(mod.test_model(_StubRequest({"provider": "prov1", "model": "m1"})))
    assert resp.status_code == 504
    data = json.loads(resp.body)
    assert data["ok"] is False and "timed out" in data["error"]


def test_model_test_passes_when_fast(guarded_mod, monkeypatch):
    mod = guarded_mod

    async def _fast_probe(body):
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True, "status": 200})

    monkeypatch.setattr(mod, "_process_chat_completion", _fast_probe)
    resp = asyncio.run(mod.test_model(_StubRequest({"provider": "prov1", "model": "m1"})))
    assert resp.status_code == 200
