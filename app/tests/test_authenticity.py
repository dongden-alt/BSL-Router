"""Tests for the universal model authenticity module."""

import asyncio
import json
import pytest
import httpx
from unittest.mock import patch, MagicMock

from app.authenticity.cache import AuthenticityCache, get_cache, reset_cache
from app.authenticity.probes import ProbeResult, TIER1_PROBES, TIER2_PROBES, run_probe
from app.authenticity.fingerprint import (
    FingerprintFeatures,
    extract_features,
    strip_self_claims,
)
from app.authenticity.classifier import classify_family, FamilyVerdict
from app.authenticity.matcher import match_submodel, MatchResult
from app.authenticity.scoring import (
    RealityVerdict,
    Modification,
    compute_reality,
    confidence_level,
    CONFIDENCE_LEVELS,
)
from app.authenticity.baseline import BaselineStore
from app.authenticity.gate import authenticity_check
from app.authenticity import scope
from app.authenticity.traitmark import TraitMark, LAYER_WEIGHTS


def _run(coro):
    """Run an async coroutine in a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def _rv(verdict="real", confidence=0.9, model_id="glm-4.6"):
    """Build a finalized RealityVerdict for cache/gate tests."""
    return RealityVerdict(
        model_id=model_id, endpoint="https://api.example.com",
        claimed=model_id, verdict=verdict, confidence=confidence,
        identified_as=model_id,
    ).finalize()


# ── Cache tests ──────────────────────────────────────────────────────


class TestAuthenticityCache:
    def test_put_and_get(self):
        cache = AuthenticityCache(ttl_seconds=60)
        cache.put("https://api.example.com", "glm-4.6", _rv("real"))
        result = cache.get("https://api.example.com", "glm-4.6")
        assert result is not None
        assert result.verdict == "real"

    def test_miss_returns_none(self):
        cache = AuthenticityCache(ttl_seconds=60)
        assert cache.get("https://api.example.com", "glm-4.6") is None

    def test_expiry(self):
        cache = AuthenticityCache(ttl_seconds=0)
        cache.put("https://api.example.com", "glm-4.6", _rv("real"))
        assert cache.get("https://api.example.com", "glm-4.6") is None

    def test_invalidate(self):
        cache = AuthenticityCache(ttl_seconds=60)
        cache.put("https://api.example.com", "glm-4.6", _rv("real"))
        cache.invalidate("https://api.example.com", "glm-4.6")
        assert cache.get("https://api.example.com", "glm-4.6") is None

    def test_invalidate_all(self):
        cache = AuthenticityCache(ttl_seconds=60)
        cache.put("https://a.com", "glm-4.6", _rv("real"))
        cache.put("https://b.com", "qwen-3", _rv("real", model_id="qwen-3"))
        cache.invalidate_all()
        assert cache.get("https://a.com", "glm-4.6") is None
        assert cache.get("https://b.com", "qwen-3") is None

    def test_transient_verdicts_not_cached(self):
        # unverifiable + error are transient → re-probe next time
        cache = AuthenticityCache(ttl_seconds=60)
        cache.put("https://api.example.com", "glm-4.6", _rv("unverifiable"))
        assert cache.get("https://api.example.com", "glm-4.6") is None
        cache.put("https://api.example.com", "glm-4.6", _rv("error"))
        assert cache.get("https://api.example.com", "glm-4.6") is None

    def test_stable_verdicts_cached(self):
        # real / downgrade / substitution / family_match are stable → cached
        cache = AuthenticityCache(ttl_seconds=60)
        for v in ("real", "downgrade", "substitution", "family_match"):
            cache.put("https://api.example.com", f"m-{v}", _rv(v, model_id=f"m-{v}"))
            assert cache.get("https://api.example.com", f"m-{v}") is not None

    def test_singleton(self):
        reset_cache()
        c1 = get_cache(1800)
        c2 = get_cache(1800)
        assert c1 is c2
        reset_cache()


# ── Probe tests ──────────────────────────────────────────────────────


def _mock_response(status_code=200, content="Hello", thinking=""):
    """Build a real httpx.Response for MockTransport."""
    body = json.dumps({
        "choices": [{"message": {"content": content, "reasoning_content": thinking}}]
    }).encode()
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
    )


class TestRunProbe:
    def test_success(self):
        _run(self._test_success())

    async def _test_success(self):
        mock_resp = _mock_response(200, "I am GLM-4.6", "thinking...")
        transport = httpx.MockTransport(lambda req: mock_resp)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(
                client, "https://api.example.com", "sk-test",
                "glm-4.6", TIER1_PROBES[0],
            )
        assert result.status_code == 200
        assert result.content == "I am GLM-4.6"
        assert result.thinking == "thinking..."
        assert result.error == ""

    def test_http_error(self):
        _run(self._test_http_error())

    async def _test_http_error(self):
        mock_resp = _mock_response(500)
        transport = httpx.MockTransport(lambda req: mock_resp)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(
                client, "https://api.example.com", "sk-test",
                "glm-4.6", TIER1_PROBES[0],
            )
        assert result.status_code == 500
        assert result.content == ""
        assert "HTTP 500" in result.error

    def test_timeout(self):
        _run(self._test_timeout())

    async def _test_timeout(self):
        async def slow_handler(request):
            await asyncio.sleep(0.5)
            return _mock_response(200)

        transport = httpx.MockTransport(slow_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(
                client, "https://api.example.com", "sk-test",
                "glm-4.6", TIER1_PROBES[0], timeout_s=1,
            )
        assert result.probe_id == TIER1_PROBES[0]["id"]

    def test_connection_error(self):
        _run(self._test_connection_error())

    async def _test_connection_error(self):
        def fail_handler(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(fail_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await run_probe(
                client, "https://api.example.com", "sk-test",
                "glm-4.6", TIER1_PROBES[0],
            )
        assert result.status_code == 0
        assert "Connection refused" in result.error

    def test_url_normalization(self):
        _run(self._test_url_normalization())

    async def _test_url_normalization(self):
        mock_resp = _mock_response(200, "ok")
        captured_urls = []

        def capture_handler(request):
            captured_urls.append(str(request.url))
            return mock_resp

        transport = httpx.MockTransport(capture_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await run_probe(
                client, "https://api.example.com", "sk-test",
                "glm-4.6", TIER1_PROBES[0],
            )
        assert captured_urls[0] == "https://api.example.com/v1/chat/completions"


# ── Fingerprint tests ────────────────────────────────────────────────


GLM_FAMILY_REF = {
    "self_keywords": ["glm", "chatglm", "z.ai", "zhipu"],
    "impostor_keywords": ["qwen", "deepseek", "moonshot", "kimi", "llama"],
}


def _make_result(probe_id, content, status_code=200):
    return ProbeResult(
        probe_id=probe_id, tier=0, status_code=status_code,
        content=content, thinking="", latency_ms=100,
    )


class TestExtractFeatures:
    def test_identity_direct_glm(self):
        results = [
            _make_result("identity_direct", "MODEL: GLM-4.6\nDEVELOPER: Zhipu AI\nCUTOFF: 2025-04\nARCH: 355B MoE"),
        ]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.claimed_model == "GLM-4.6"
        assert features.claimed_org == "Zhipu AI"
        assert "glm" in features.self_hits
        assert len(features.impostor_hits) == 0

    def test_identity_direct_kimi_impostor(self):
        results = [
            _make_result("identity_direct", "MODEL: Kimi K2\nDEVELOPER: Moonshot AI\nCUTOFF: 2025-01\nARCH: 32B"),
        ]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.claimed_model == "Kimi K2"
        assert "kimi" in features.impostor_hits
        assert "moonshot" in features.impostor_hits

    def test_identity_adversarial_unknown_count(self):
        results = [
            _make_result("identity_adversarial", "I don't know my parameter count. unknown unknown unknown."),
        ]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.unknown_count == 3

    def test_style_sample_features(self):
        content = (
            "Imagine a transformer as a very smart reader.\n"
            "It reads all the words at once, like scanning a page.\n"
            "- It pays attention to important words.\n"
            "- It builds understanding layer by layer.\n"
            "Think of it like a detective gathering clues."
        )
        results = [_make_result("style_sample", content)]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.bullet_style == "-"
        assert features.has_analogy is True
        assert features.sentence_count > 0
        assert features.response_length == len(content)

    def test_cutoff_probe_extraction(self):
        results = [_make_result("cutoff_probe", "My training cutoff is April 2025.")]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.cutoff_year == 2025
        assert features.cutoff_month == 4

    def test_cutoff_probe_numeric(self):
        results = [_make_result("cutoff_probe", "2025-04")]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.cutoff_year == 2025
        assert features.cutoff_month == 4

    def test_capability_boundary(self):
        results = [_make_result("capability_boundary", "1. Paris  2. 391  3. 2007")]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.capability_correct == 3

    def test_capability_boundary_wrong(self):
        results = [_make_result("capability_boundary", "1. London  2. 400  3. 2010")]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.capability_correct == 0

    def test_error_results_skipped(self):
        results = [
            _make_result("identity_direct", "", status_code=500),
            _make_result("identity_direct", "MODEL: GLM-4.6\nDEVELOPER: Zhipu"),
        ]
        features = extract_features(results, GLM_FAMILY_REF)
        assert features.claimed_model == "GLM-4.6"


class TestStripSelfClaims:
    def test_removes_i_am_pattern(self):
        text = "I am a large language model trained by Zhipu AI. I can help you with tasks."
        result = strip_self_claims(text)
        assert "I am a large language model" not in result
        assert "help you with tasks" in result

    def test_removes_model_field(self):
        text = "MODEL: GLM-4.6\nDEVELOPER: Zhipu AI\nI can answer questions."
        result = strip_self_claims(text)
        assert "MODEL: GLM-4.6" not in result
        assert "answer questions" in result

    def test_preserves_non_identity_content(self):
        text = "The capital of France is Paris. It has been the capital since 987 CE."
        result = strip_self_claims(text)
        assert "capital of France is Paris" in result


# ── Classifier tests ─────────────────────────────────────────────────


MOCK_FAMILIES = {
    "glm": {
        "family": "glm",
        "self_keywords": ["glm"],
        "impostor_keywords": ["kimi", "qwen"],
        "style_baseline": {
            "bullet_style": "-", "cjk_ratio": 0.05,
            "avg_sentence_length": 80, "header_depth": 2,
            "has_analogy": True, "question_count": 2,
        },
    },
    "kimi": {
        "family": "kimi",
        "self_keywords": ["kimi"],
        "impostor_keywords": ["glm", "qwen"],
        "style_baseline": {
            "bullet_style": "-", "cjk_ratio": 0.04,
            "avg_sentence_length": 95, "header_depth": 2,
            "has_analogy": True, "question_count": 1,
        },
    },
}


class TestClassifyFamily:
    def test_genuine_glm(self):
        features = FingerprintFeatures(
            claimed_model="GLM-4.6", claimed_org="Zhipu AI",
            self_hits=["glm"], impostor_hits=[],
            bullet_style="-", cjk_ratio=0.05, avg_sentence_length=80,
            header_depth=2, has_analogy=True, question_count=2,
        )
        verdict = classify_family(features, MOCK_FAMILIES)
        assert verdict.surface_family == "claimed"
        assert verdict.injection_detected is False

    def test_kimi_impostor(self):
        features = FingerprintFeatures(
            claimed_model="Kimi K2", claimed_org="Moonshot AI",
            self_hits=[], impostor_hits=["kimi"],
        )
        verdict = classify_family(features, MOCK_FAMILIES)
        assert verdict.surface_family == "impostor"
        assert verdict.confidence >= 0.9

    def test_mixed_signals(self):
        features = FingerprintFeatures(
            claimed_model="GLM-4.6", claimed_org="Zhipu",
            self_hits=["glm"], impostor_hits=["kimi"],
        )
        verdict = classify_family(features, MOCK_FAMILIES)
        assert verdict.surface_family == "mixed"

    def test_no_identity_info(self):
        features = FingerprintFeatures()
        verdict = classify_family(features, MOCK_FAMILIES)
        assert verdict.surface_family == "unknown"
        assert verdict.confidence < 0.5


# ── Matcher tests ────────────────────────────────────────────────────


MOCK_BASELINES = {
    "glm_glm-4.5": {
        "model_id": "glm-4.5", "family": "glm",
        "cutoff_year": 2024, "cutoff_month": 11, "capability_correct": 3,
    },
    "glm_glm-4.6": {
        "model_id": "glm-4.6", "family": "glm",
        "cutoff_year": 2025, "cutoff_month": 4, "capability_correct": 3,
    },
}


class TestMatchSubmodel:
    def test_match_glm_4_6(self):
        features = FingerprintFeatures(cutoff_year=2025, cutoff_month=4, capability_correct=3)
        result = match_submodel(features, MOCK_BASELINES)
        assert result.matched_model == "glm_glm-4.6"
        assert result.match_score >= 0.6
        assert result.abstain is False

    def test_match_glm_4_5(self):
        features = FingerprintFeatures(cutoff_year=2024, cutoff_month=11, capability_correct=3)
        result = match_submodel(features, MOCK_BASELINES)
        assert result.matched_model == "glm_glm-4.5"
        assert result.abstain is False

    def test_abstain_on_no_baselines(self):
        features = FingerprintFeatures(cutoff_year=2025, cutoff_month=4, capability_correct=3)
        result = match_submodel(features, {})
        assert result.abstain is True

    def test_abstain_on_insufficient_gap(self):
        close_baselines = {
            "a": {"cutoff_year": 2025, "cutoff_month": 4, "capability_correct": 3},
            "b": {"cutoff_year": 2025, "cutoff_month": 4, "capability_correct": 3},
        }
        features = FingerprintFeatures(cutoff_year=2025, cutoff_month=4, capability_correct=3)
        result = match_submodel(features, close_baselines)
        assert result.abstain is True

    def test_unknown_cutoff_neutral(self):
        features = FingerprintFeatures(cutoff_year=0, cutoff_month=0, capability_correct=3)
        result = match_submodel(features, MOCK_BASELINES)
        assert isinstance(result, MatchResult)


# ── Scoring tests ────────────────────────────────────────────────────


def _scope(vs="full", family="glm", focus=""):
    return {"verdict_scope": vs, "family": family, "focus": focus, "reason": ""}


class TestConfidenceLevel:
    def test_five_levels(self):
        assert confidence_level(0.10) == "very_low"
        assert confidence_level(0.40) == "low"
        assert confidence_level(0.65) == "medium"
        assert confidence_level(0.82) == "high"
        assert confidence_level(0.95) == "very_high"

    def test_levels_list(self):
        assert CONFIDENCE_LEVELS == [
            "very_low", "low", "medium", "high", "very_high"]


class TestComputeReality:
    def _fv(self, surface="claimed", behavior="glm", family="claimed",
            conf=0.9, injection=False):
        return FamilyVerdict(
            family=family, confidence=conf, surface_family=surface,
            behavior_family=behavior, injection_detected=injection,
            evidence={},
        )

    def _mr(self, matched="glm_glm-4.6", score=0.9, abstain=False):
        return MatchResult(
            matched_model=matched, match_score=score,
            runner_up="glm_glm-4.5", runner_up_score=0.5,
            abstain=abstain, dimensions={},
        )

    def test_real_verdict(self):
        v = compute_reality("glm-4.6", "https://api.example.com", "glm-4.6",
                            _scope("full", "glm", "glm-4.6"),
                            self._fv(), self._mr(), [], [], 5000)
        assert v.verdict == "real"
        assert v.confidence >= 0.8
        assert v.confidence_level == "very_high"

    def test_substitution_on_impostor(self):
        fv = self._fv(surface="impostor", behavior="kimi", family="impostor")
        fv.evidence = {"impostor_hits": ["kimi"]}
        v = compute_reality("glm-4.6", "https://api.example.com", "glm-4.6",
                            _scope("full", "glm", "glm-4.6"),
                            fv, None, [], [], 3000)
        assert v.verdict == "substitution"
        assert v.identified_as == "kimi"

    def test_injection_is_modification_not_fake(self):
        fv = self._fv(behavior="kimi", injection=True)
        v = compute_reality("glm-4.6", "https://api.example.com", "glm-4.6",
                            _scope("full", "glm", "glm-4.6"),
                            fv, self._mr(), [], [], 3000)
        mod_types = [m.type for m in v.modifications]
        assert "system_prompt_injection" in mod_types

    def test_family_match_scope(self):
        # family scope, no sub-model standard → family_match
        v = compute_reality("glm-4.9", "https://api.example.com", "glm-4.9",
                            _scope("family", "glm"),
                            self._fv(conf=0.7), None, [], [], 3000)
        assert v.verdict == "family_match"

    def test_unverifiable_out_of_scope(self):
        v = compute_reality("llama-3-8b", "https://api.example.com", "llama-3-8b",
                            _scope("unknown", "", ""),
                            None, None, [], [], 100)
        assert v.verdict == "unverifiable"

    def test_error_when_all_probes_failed(self):
        v = compute_reality("glm-4.6", "https://api.example.com", "glm-4.6",
                            _scope("full", "glm", "glm-4.6"),
                            None, None, [], ["identity_direct", "style"], 100)
        assert v.verdict == "error"

    def test_downgrade_on_submodel_mismatch(self):
        # hinted 4.6 but matched a lower sub-model
        fv = self._fv(behavior="glm")
        mr = self._mr(matched="glm_glm-4.5", score=0.9)
        v = compute_reality("glm-4.6", "https://api.example.com", "glm-4.6",
                            _scope("full", "glm", "glm-4.6"),
                            fv, mr, [], [], 3000)
        assert v.verdict == "downgrade"
        assert v.identified_as == "glm_glm-4.5"

    def test_headline_and_to_dict(self):
        v = compute_reality("glm-4.6", "https://api.example.com", "glm-4.6",
                            _scope("full", "glm", "glm-4.6"),
                            self._fv(), self._mr(), [], [], 3000)
        d = v.to_dict()
        assert d["verdict"] == "real"
        assert "confidence_level" in d
        assert isinstance(v.headline(), str)
        assert v.timestamp != ""


# ── Baseline tests ───────────────────────────────────────────────────


class TestBaselineStore:
    def test_load(self, tmp_path):
        baseline = {"model_id": "glm-4.6", "cutoff_year": 2025}
        (tmp_path / "glm_glm-4.6_baseline.json").write_text(json.dumps(baseline))
        store = BaselineStore(tmp_path)
        result = store.load("glm-4.6")
        assert result is not None
        assert result["model_id"] == "glm-4.6"

    def test_load_missing(self, tmp_path):
        store = BaselineStore(tmp_path)
        assert store.load("nonexistent") is None

    def test_load_family(self, tmp_path):
        (tmp_path / "glm_glm-4.5_baseline.json").write_text(json.dumps({"model_id": "glm-4.5"}))
        (tmp_path / "glm_glm-4.6_baseline.json").write_text(json.dumps({"model_id": "glm-4.6"}))
        (tmp_path / "qwen_qwen-3_baseline.json").write_text(json.dumps({"model_id": "qwen-3"}))
        store = BaselineStore(tmp_path)
        results = store.load_family("glm")
        assert len(results) == 2
        assert "glm_glm-4.5" in results
        assert "glm_glm-4.6" in results

    def test_is_stale(self, tmp_path):
        baseline = {"model_id": "glm-4.6"}
        (tmp_path / "glm_glm-4.6_baseline.json").write_text(json.dumps(baseline))
        store = BaselineStore(tmp_path)
        store.load("glm-4.6")
        assert store.is_stale("glm-4.6", max_age_days=30) is False
        assert store.is_stale("glm-4.6", max_age_days=0) is True

    def test_list_models(self, tmp_path):
        (tmp_path / "glm_glm-4.6_baseline.json").write_text(json.dumps({}))
        (tmp_path / "qwen_qwen-3_baseline.json").write_text(json.dumps({}))
        store = BaselineStore(tmp_path)
        models = store.list_models()
        assert len(models) == 2


# ── Gate tests ───────────────────────────────────────────────────────


class TestAuthenticityGate:
    def test_unverifiable_when_out_of_scope(self):
        # Unknown model, no reference hint → honest UNVERIFIABLE (never "fake")
        verdict = _run(authenticity_check(
            model_id="unknown-model-xyz",
            endpoint_base="https://api.example.com",
            api_key="sk-test",
            config={"cache_ttl_seconds": 1800},
        ))
        assert verdict.verdict == "unverifiable"

    def test_fail_open_on_exception(self):
        with patch("app.authenticity.gate._load_family_ref",
                   side_effect=ValueError("boom")):
            verdict = _run(authenticity_check(
                model_id="glm-4.6",
                endpoint_base="https://api.example.com",
                api_key="sk-test",
                config={"cache_ttl_seconds": 1800},
                reference_hint="glm-4.6",  # force in-scope so _load_family_ref runs
            ))
        assert verdict.verdict == "error"

    def test_cache_hit(self):
        reset_cache()
        cache = get_cache(1800)
        cache.put("https://api.example.com", "glm-4.6", _rv("real"))

        verdict = _run(authenticity_check(
            model_id="glm-4.6",
            endpoint_base="https://api.example.com",
            api_key="sk-test",
            config={"cache_ttl_seconds": 1800},
        ))
        assert verdict.verdict == "real"
        assert verdict.confidence == 0.9
        reset_cache()


# ── Scope tests ────────────────────────────────────────────────────────────


class TestScope:
    def test_recognized_family_full(self):
        info = scope.in_scope("glm-5.3")
        assert info["family"] == "glm"

    def test_reference_hint_narrows_ambiguous(self):
        info = scope.in_scope("ox-alpha", "glm-5.3-flash")
        assert info["verdict_scope"] == "full"
        assert info["family"] == "glm"
        assert info["focus"] == "glm-5.3-flash"

    def test_unknown_model_unknown_scope(self):
        info = scope.in_scope("falcon-40b", "")
        assert info["verdict_scope"] == "unknown"

    def test_wrong_hint_not_fail_closed(self):
        # A wrong hint must NOT produce a fake verdict — it biases the focus
        # to the hint as a hypothesis (flagged in reason), never fail-closed.
        info = scope.in_scope("glm-5.3", "kimi-k2")
        assert info["verdict_scope"] != "unknown"
        assert info["family"] == "kimi"          # hint is the hypothesis tested first
        assert "hint" in info["reason"].lower()  # disagreement is flagged

    def test_detect_family_variants(self):
        assert scope.detect_family("gpt-6.1-sol") == "gpt"
        assert scope.detect_family("qwen3.8-max-0902") == "qwen"
        assert scope.detect_family("deepseek-v4.1-flash") == "deepseek"
        assert scope.detect_family("mimo-v2.6-pro") == "mimo"
        assert scope.detect_family("claude-opus-5") == "claude"
        assert scope.detect_family("gemini-3-pro") == "gemini"
        assert scope.detect_family("kimi-k3") == "kimi"


# ── TraitMark tests ────────────────────────────────────────────────────────


class TestTraitMark:
    def test_roundtrip(self):
        t = TraitMark()
        t.meta.model_id = "glm-4.6"
        t.meta.family = "glm"
        d = t.to_dict()
        t2 = TraitMark.from_dict(d)
        assert t2.meta.model_id == "glm-4.6"
        assert t2.meta.family == "glm"

    def test_layer_weights(self):
        # 7 weighted layers, normalized to 1.0, knowledge is strongest
        assert len(LAYER_WEIGHTS) == 7
        assert abs(sum(LAYER_WEIGHTS.values()) - 1.0) < 1e-6
        assert max(LAYER_WEIGHTS, key=LAYER_WEIGHTS.get) == "knowledge"

    def test_has_nine_layers(self):
        d = TraitMark().to_dict()
        assert len(d) == 9
