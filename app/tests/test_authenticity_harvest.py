"""Tests for the standards harvester (app/authenticity/harvest.py).

conftest.py already sets BSL_CONFIG_PATH to a session temp config BEFORE
importing app.main, so TestClient(main.app) is safe here.
All network is mocked via httpx.MockTransport injected through the
harvest._make_client seam — no real upstream is ever contacted.
"""
import asyncio
import json

import httpx
import pytest

import app.authenticity.harvest as harvest
from app.authenticity.harvest import (
    NONCODING_PROBES, JUDGE_AXES, judge_scores, harvest_model,
    save_standard, list_standards, delete_standard, _strip_json_fences,
)

# Lifespan startup ~10.5s (port-443 inspection) exceeds the 10s global budget.
pytestmark = pytest.mark.timeout(60)


def _run(coro):
    """Run an async coroutine in a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# ── Mock endpoints ───────────────────────────────────────────────────────────

def _chat_response(content, thinking=""):
    msg = {"content": content}
    if thinking:
        msg["reasoning_content"] = thinking
    return httpx.Response(200, json={
        "choices": [{"message": msg}],
        "usage": {"total_tokens": 10},
    })


def _anchor_transport(model_id="glm-4.6"):
    """Trusted anchor: answers every probe; judge URLs return clean JSON."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content.decode() or "{}")
        prompt = (body.get("messages") or [{}])[-1].get("content", "")
        if "judge" in url:
            return _chat_response(json.dumps({a: {"score": 8, "why": "solid"}
                                              for a in JUDGE_AXES}))
        if "cutoff" in prompt.lower():
            return _chat_response("2026-04")
        if "Identify yourself" in prompt:
            return _chat_response(
                f"MODEL: {model_id}\nDEVELOPER: Zhipu\nCUTOFF: 2026-04\nARCH: 355B MoE")
        if "capital of France" in prompt:
            return _chat_response("1. Paris  2. 391  3. 2007")
        return _chat_response("A transformer uses attention to weigh tokens. " * 8)
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_anchor(monkeypatch):
    def make():
        return httpx.AsyncClient(transport=_anchor_transport())
    monkeypatch.setattr(harvest, "_make_client", make)
    return make


# ── judge_scores ─────────────────────────────────────────────────────────────

def test_strip_json_fences():
    assert _strip_json_fences('{"a":1}') == '{"a":1}'
    assert _strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_json_fences('Here you go: {"a":1} done') == '{"a":1}'


def test_judge_scores_clean_json(mock_anchor):
    outs = [{"axis": "creative", "id": "creative_story", "content": "story..."}]
    res = _run(judge_scores("https://judge.example", "k", "judge-model", outs))
    assert res["creative"]["score"] == 8
    assert res["creative"]["why"] == "solid"
    for a in JUDGE_AXES:
        assert a in res


def test_judge_scores_judge_down(monkeypatch):
    def make():
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(500, text="down")))
    monkeypatch.setattr(harvest, "_make_client", make)
    outs = [{"axis": "creative", "id": "x", "content": "s"}]
    res = _run(judge_scores("https://judge.example", "k", "judge-model", outs))
    assert all(v["score"] == 0 and v["why"] == "judge_error"
               for v in res.values())


def test_judge_scores_no_creds():
    res = _run(judge_scores("", "", "", []))
    assert all(v["why"] == "judge_error" for v in res.values())


# ── harvest_model ────────────────────────────────────────────────────────────

def test_harvest_model_with_judge(mock_anchor):
    tm = _run(harvest_model(
        "https://api.example.com", "sk-test", "glm-4.6",
        judge_endpoint="https://judge.example", judge_key="k",
        judge_model="judge-model", include_noncoding=True))
    assert tm.meta.family == "glm"
    assert tm.knowledge.cutoff_year == 2026
    assert tm.knowledge.cutoff_month == 4
    # Judge was provided, so no judge/noncoding gap. The mock anchor returns no
    # logprobs and no needle self-report, so the V1/V5 gaps ARE expected here.
    assert not any("noncoding" in g or "judge" in g
                   for g in tm.meta.harvest_gaps)
    assert any("logprobs" in g for g in tm.meta.harvest_gaps)
    assert tm.noncoding.creative["score"] == 8


def test_harvest_model_no_judge(mock_anchor):
    tm = _run(harvest_model(
        "https://api.example.com", "sk-test", "glm-4.6",
        include_noncoding=True))
    # Noncoding graded axes stay at defaults; gap recorded.
    assert tm.noncoding.creative == {} or tm.noncoding.creative.get("score", 0) == 0
    assert any("noncoding" in g for g in tm.meta.harvest_gaps)


def test_harvest_model_anchor_down_failopen(monkeypatch):
    def make():
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(503, text="down")))
    monkeypatch.setattr(harvest, "_make_client", make)
    tm = _run(harvest_model("https://api.example.com", "sk-test", "glm-4.6"))
    # Never raises; gaps recorded.
    assert isinstance(tm.meta.harvest_gaps, list)
    assert len(tm.meta.harvest_gaps) > 0


# ── persistence roundtrip ────────────────────────────────────────────────────

def test_save_list_delete_roundtrip(tmp_path):
    from app.authenticity.traitmark import TraitMark
    tm = TraitMark()
    tm.meta.model_id = "glm-4.6"
    tm.meta.family = "glm"
    tm.meta.sub_model = "glm-4.6"
    p = save_standard(tm, tmp_path)
    assert p.name == "glm_glm-4-6.json"  # sanitizer strips dots
    assert not list(tmp_path.glob(".tmp_*.json"))  # atomic: no temp left

    lst = list_standards(tmp_path)
    assert len(lst) == 1
    assert lst[0]["family"] == "glm"
    assert lst[0]["sub_model"] == "glm-4.6"

    assert delete_standard(tmp_path, "glm", "glm-4.6") is True
    assert list_standards(tmp_path) == []
    assert delete_standard(tmp_path, "glm", "glm-4.6") is False


# ── admin endpoints ──────────────────────────────────────────────────────────

def test_standards_list_endpoint(tmp_path, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.auth_harvest, "STANDARDS_DIR", tmp_path)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        r = client.get("/api/authenticity/standards")
        assert r.status_code == 200
        assert isinstance(r.json().get("standards"), list)


def test_harvest_endpoint_starts_and_validates(tmp_path, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.auth_harvest, "STANDARDS_DIR", tmp_path)
    monkeypatch.setattr(harvest, "_make_client",
                        lambda: httpx.AsyncClient(transport=_anchor_transport()))
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        # Missing fields → 400
        # 400 contract: only a missing model_id is rejected; endpoint/api_key
        # come from the router self-endpoint, so their absence is NOT a 400.
        bad = client.post("/api/authenticity/standards/harvest", json={})
        assert bad.status_code == 400
        # Valid → started. Uses the STANDARD glm lane (aibox/ZHIPU/) so both the
        # trusted-source guard and the 2026-10-08 lane-policy guard admit it.
        ok = client.post("/api/authenticity/standards/harvest", json={
            "model_id": "aibox/ZHIPU/GLM-5.3", "endpoint": "https://api.example.com",
            "api_key": "sk-test", "include_noncoding": False})
        assert ok.status_code == 200
        assert ok.json()["started"] is True
        # Status endpoint responds
        st = client.get("/api/authenticity/standards/harvest/status/aibox/ZHIPU/GLM-5.3")
        assert st.status_code == 200
        assert st.json()["status"] in ("running", "done", "error")


def test_harvest_trusted_source_guard(tmp_path, monkeypatch):
    """Canonical harvests (source_tag empty) may only come from trusted sources
    (codex/antigravity/mintrouter/aibox). Untrusted lanes are refused with 400;
    a provider-tagged snapshot (source_tag set) is exempt and may proceed."""
    import app.main as main
    monkeypatch.setattr(main.auth_harvest, "STANDARDS_DIR", tmp_path)
    monkeypatch.setattr(harvest, "_make_client",
                        lambda: httpx.AsyncClient(transport=_anchor_transport()))
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        # Untrusted canonical harvest → 400 refused
        for lane in ("vsllm-o/glm-5.3", "a6api/deepseek-v4-pro", "glm-4.6"):
            r = client.post("/api/authenticity/standards/harvest", json={
                "model_id": lane, "endpoint": "https://api.example.com",
                "api_key": "sk-test", "include_noncoding": False})
            assert r.status_code == 400, f"{lane} must be refused"
            assert r.json().get("refused") is True
        # Untrusted lane WITH a source_tag → exempt (snapshot, not canonical) → started
        snap = client.post("/api/authenticity/standards/harvest", json={
            "model_id": "vsllm-o/glm-5.3", "source_tag": "glm-5.3",
            "endpoint": "https://api.example.com", "api_key": "sk-test",
            "include_noncoding": False})
        assert snap.status_code == 200
        assert snap.json()["started"] is True
