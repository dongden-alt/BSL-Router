"""Regression tests for the 2026-10-08 canonical-standard audit fixes.

Each test pins a defect that was reproduced by execution against the real
frozen standards (see .brain/CHANGELOG.md 2026-10-08 audit entry):

  * needle dimension crashed (TypeError: unhashable dict) on production-shaped
    standards and the gate swallowed it fail-open  -> zero test coverage
  * needle recall was denominated over facts the suspect was never asked
  * candidates were ranked on cutoff+capability only -> siblings tied and the
    FIRST ALPHABETICAL key won (the "opus-5-5 detected as fable-5-1" bug)
  * stability veto was un-satisfiable for non-deterministic (temp=0) models
  * judge tie-break candidate summary read flat keys absent from TraitMark
"""
from app.authenticity.fingerprint import FingerprintFeatures
from app.authenticity.judge_rank import _candidate_summary
from app.authenticity.knowledge_needles import (
    classify_needle_answers, collect_family_needles, fact_text,
    is_disclaimer_fact,
)
from app.authenticity.matcher import match_submodel

DISCLAIMER = ("I don't know my training-data cutoff precisely. These are the "
              "most recent events I can state with confidence")


def _std(known=(), post=(), nc=None, cutoff=0, cap=3, stability=None):
    """TraitMark-shaped standard; needles stored as {fact, needle} dicts."""
    s = {
        "knowledge": {
            "cutoff_year": cutoff,
            "cutoff_month": 0,
            "known_facts": [{"fact": f, "needle": f.split()[-1]} for f in known],
            "post_cutoff_unknown": [{"fact": f, "needle": f} for f in post],
            "capability_boundary": {"correct": cap},
        },
        "noncoding": nc or {},
    }
    if stability:
        s["structural"] = {"tokenizer_signature": {"stability_profile": stability}}
    return s


def _needle_features(items, answers_by_fact):
    """Build suspect features the way the gate does: via classify_needle_answers."""
    needles = [dict(fact=f, needle=n, kind=k, source="x") for f, n, k in items]
    text = "\n".join(f"{i}. {answers_by_fact.get(n['fact'], 'unknown')}"
                     for i, n in enumerate(needles, 1))
    feats = FingerprintFeatures(capability_correct=3)
    feats.knowledge_needles = classify_needle_answers(text, needles)
    return feats


class TestNeedleDimension:
    def test_dict_needles_do_not_crash(self):
        # Production shape: dict needles in the standard, str hits on the suspect.
        base = _std(known=["alpha event Zeta", "beta event Omega"],
                    post=["gamma future event"])
        feats = _needle_features(
            [("alpha event Zeta", "Zeta", "known"),
             ("beta event Omega", "Omega", "known"),
             ("gamma future event", "gamma future event", "post_cutoff")],
            {"alpha event Zeta": "it was Zeta", "beta event Omega": "Omega"})
        res = match_submodel(feats, {"m": base})  # used to raise TypeError
        assert res.dimensions["knowledge"] == 1.0

    def test_unasked_facts_are_not_penalised(self):
        # Standard has 3 known facts; the probe only sampled 2 (both hit).
        base = _std(known=["f1 Aone", "f2 Btwo", "f3 Cthree"])
        feats = _needle_features(
            [("f1 Aone", "Aone", "known"), ("f2 Btwo", "Btwo", "known")],
            {"f1 Aone": "Aone", "f2 Btwo": "Btwo"})
        res = match_submodel(feats, {"m": base})
        assert res.dimensions["knowledge"] == 1.0  # old code: 2/3

    def test_wrong_version_scores_low(self):
        # Suspect misses the known facts and claims the post-cutoff one.
        base = _std(known=["k1 Aone", "k2 Btwo"], post=["p1 future"])
        feats = _needle_features(
            [("k1 Aone", "Aone", "known"), ("k2 Btwo", "Btwo", "known"),
             ("p1 future", "p1 future", "post_cutoff")],
            {"k1 Aone": "dunno-ish", "k2 Btwo": "nope", "p1 future": "it happened"})
        res = match_submodel(feats, {"m": base})
        assert res.dimensions["knowledge"] == 0.0

    def test_single_scoped_item_is_not_evidence(self):
        base = _std(known=["only Aone"])
        feats = _needle_features([("only Aone", "Aone", "known")], {"only Aone": "Aone"})
        assert match_submodel(feats, {"m": base}).dimensions["knowledge"] is None

    def test_disclaimer_fact_is_ignored(self):
        assert is_disclaimer_fact(DISCLAIMER)
        assert not is_disclaimer_fact("Pope Francis died at Casa Santa Marta")
        base = _std(known=[DISCLAIMER, "real1 Aone", "real2 Btwo"])
        feats = _needle_features(
            [("real1 Aone", "Aone", "known"), ("real2 Btwo", "Btwo", "known")],
            {"real1 Aone": "Aone", "real2 Btwo": "Btwo"})
        assert match_submodel(feats, {"m": base}).dimensions["knowledge"] == 1.0

    def test_collect_family_needles_skips_disclaimer_and_normalises(self):
        out = collect_family_needles({"m": _std(known=[DISCLAIMER, "ok Aone"])})
        assert [n["fact"] for n in out] == ["ok Aone"]
        assert fact_text({"fact": " x ", "needle": "y"}) == "x"
        assert fact_text("  plain ") == "plain"


class TestRanking:
    def _nc(self, v):
        return {a: {"score": v} for a in ("creative", "eq", "humor", "design")}

    def test_siblings_ranked_on_full_evidence_not_alphabetically(self):
        # Identical cutoff (unknown) + identical capability -> the 2-dim base
        # ties. The OLD matcher returned the first alphabetical key ("a_sib").
        pool = {"a_sib": _std(nc=self._nc(3)), "z_sib": _std(nc=self._nc(8))}
        feats = FingerprintFeatures(capability_correct=3)
        feats.noncoding = {"judge": {a: {"score": 8} for a in
                                     ("creative", "eq", "humor", "design")}}
        res = match_submodel(feats, pool)
        assert res.best_model == "z_sib"
        assert res.matched_model == "z_sib" and res.abstain is False

    def test_true_tie_still_abstains(self):
        pool = {"a": _std(nc=self._nc(7)), "b": _std(nc=self._nc(7))}
        feats = FingerprintFeatures(capability_correct=3)
        feats.noncoding = {"judge": {a: {"score": 7} for a in
                                     ("creative", "eq", "humor", "design")}}
        assert match_submodel(feats, pool).abstain is True

    def test_runner_up_scored_on_winners_basis(self):
        pool = {"a_sib": _std(nc=self._nc(3)), "z_sib": _std(nc=self._nc(8))}
        feats = FingerprintFeatures(capability_correct=3)
        feats.noncoding = {"judge": {a: {"score": 8} for a in
                                     ("creative", "eq", "humor", "design")}}
        res = match_submodel(feats, pool)
        assert res.match_score > res.runner_up_score
        # gap must reflect the real 0.5-vs-1.0 noncoding split, not a 2-dim tie
        assert res.match_score - res.runner_up_score >= 0.05


class TestStabilityNondeterministic:
    def _st(self, mode, distinct=3):
        return {"probe_id": "stability_poem_sea", "samples": 3,
                "distinct_hashes": distinct, "hashes": [mode] * 3, "mode_hash": mode}

    def test_both_nondeterministic_is_not_compared(self):
        base = _std(stability=self._st("h1"))
        feats = FingerprintFeatures(capability_correct=3)
        feats.stability_profile = self._st("h2")
        res = match_submodel(feats, {"m": base})
        assert res.dimensions["stability"] is None  # old: 0.4 -> veto vs GENUINE model

    def test_determinism_class_mismatch_not_compared(self):
        # FIX K (2026-10-13): was 0.0 + veto. Live run 3: genuine glm-5.3 on
        # AI-BOX (same provider as its distinct=1 gold standard) came back
        # non-deterministic -> class mismatch is not version evidence.
        base = _std(stability=self._st("h1", distinct=1))
        feats = FingerprintFeatures(capability_correct=3)
        feats.stability_profile = self._st("h2", distinct=3)
        assert match_submodel(feats, {"m": base}).dimensions["stability"] is None

    def test_both_deterministic_different_content_still_scored(self):
        base = _std(stability=self._st("h1", distinct=1))
        feats = FingerprintFeatures(capability_correct=3)
        feats.stability_profile = self._st("h2", distinct=1)
        assert match_submodel(feats, {"m": base}).dimensions["stability"] == 0.4


class TestJudgeSummary:
    def test_candidate_summary_reads_nested_traitmark_keys(self):
        std = _std(known=["k Aone"], cutoff=2025, cap=3,
                   nc={"creative": {"score": 6}})
        std["knowledge"]["cutoff_month"] = 4
        s = _candidate_summary("m", std)
        assert s["cutoff_year"] == 2025 and s["cutoff_month"] == 4
        assert s["capability_correct"] == 3  # old code: None (flat-key read)
        assert s["noncoding"] == {"creative": 6}

    def test_candidate_summary_flat_legacy_still_works(self):
        s = _candidate_summary("m", {"cutoff_year": 2024, "cutoff_month": 11,
                                     "capability_correct": 2})
        assert (s["cutoff_year"], s["capability_correct"]) == (2024, 2)


# ── FIX D (2026-10-13): probe FAILURES must never score as observed behavior ──

class TestFixDFailuresAreNotEvidence:
    def test_stability_probe_partial_failure_yields_no_profile(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace
        import app.authenticity.probes as probes_mod

        rs = iter([SimpleNamespace(status_code=200, content="a poem"),
                   SimpleNamespace(status_code=502, content=""),
                   SimpleNamespace(status_code=502, content="")])

        async def fake_run_probe(*a, **k):
            return next(rs)

        monkeypatch.setattr(probes_mod, "run_probe", fake_run_probe)
        prof = asyncio.run(probes_mod.run_stability_probe(None, "http://x", "k", "m"))
        assert prof["valid_samples"] == 1
        assert prof["mode_hash"] == ""  # old: a mode hash -> fake "deterministic"

    def test_stability_single_valid_sample_not_compared(self):
        base = _std(stability={"probe_id": "stability_poem_sea", "samples": 3,
                               "distinct_hashes": 3, "mode_hash": "h1",
                               "hashes": ["h1", "h2", "h3"]})
        feats = FingerprintFeatures(capability_correct=3)
        # legacy-shaped live profile: 2/3 samples timed out
        feats.stability_profile = {"probe_id": "stability_poem_sea", "samples": 3,
                                   "distinct_hashes": 1, "mode_hash": "z",
                                   "hashes": ["z", "", ""]}
        assert match_submodel(feats, {"m": base}).dimensions["stability"] is None  # old: 0.0

    def test_unanswered_needle_items_not_scored_as_misses(self):
        base = _std(known=["k1 Aone", "k2 Btwo", "k3 Cthree"])
        needles = [dict(fact=f, needle=n, kind="known", source="x") for f, n in
                   (("k1 Aone", "Aone"), ("k2 Btwo", "Btwo"), ("k3 Cthree", "Cthree"))]
        feats = FingerprintFeatures(capability_correct=3)
        # reply truncated after item 2 -> item 3 has no answer line at all
        feats.knowledge_needles = classify_needle_answers("1. Aone\n2. Btwo", needles)
        assert match_submodel(feats, {"m": base}).dimensions["knowledge"] == 1.0  # old: 0.667

    def test_explicit_unknown_still_counts_as_miss(self):
        base = _std(known=["k1 Aone", "k2 Btwo"])
        needles = [dict(fact=f, needle=n, kind="known", source="x") for f, n in
                   (("k1 Aone", "Aone"), ("k2 Btwo", "Btwo"))]
        feats = FingerprintFeatures(capability_correct=3)
        feats.knowledge_needles = classify_needle_answers("1. Aone\n2. unknown", needles)
        assert match_submodel(feats, {"m": base}).dimensions["knowledge"] == 0.5

    def test_failed_stop_probe_not_scored(self):
        from app.authenticity.probes import compute_boundary_signature
        from types import SimpleNamespace
        sig = compute_boundary_signature(
            None, SimpleNamespace(status_code=200,
                                  content='{"name": "a", "count": 1, "tags": ["x", "y", "z"]}'))
        assert sig["stop_mode"] == "error" and sig["json_strict"] is True
        base = _std()
        base["structural"] = {"tokenizer_signature": {"boundary_signature": {
            "stop_strict": False, "stop_echo": True, "finish_reason": "stop",
            "json_strict": True, "json_mode": "supported"}}}
        feats = FingerprintFeatures(capability_correct=3)
        feats.boundary_signature = sig
        # old: stop_echo False(default) vs True -> 0.667; now only json compared
        assert match_submodel(feats, {"m": base}).dimensions["boundary"] == 1.0

    def test_refusal_axis_with_failed_probe_dropped_from_delta(self):
        from app.authenticity.probes import REFUSAL_PROBES, compute_refusal_signature
        from types import SimpleNamespace
        results = {p["id"]: SimpleNamespace(status_code=200, content="A" * 1200)
                   for p in REFUSAL_PROBES}
        cn_zh = next(p["id"] for p in REFUSAL_PROBES
                     if p["axis"] == "cn" and p["lang"] == "zh")
        results[cn_zh] = None  # timed out
        sig = compute_refusal_signature(results)
        assert "cn" not in sig["delta"]          # old: deflated / fabricated delta
        assert sig["delta"].get("west") == 0.0   # fully captured axis kept


# ── FIX E-K (2026-10-13): replay of live run 3 ──────────────────────────────

_PROBE = {"id": "t_probe", "prompt": "hi", "max_tokens": 16}


def _sse_run(body: bytes):
    import asyncio
    import httpx
    from app.authenticity.probes import _run_probe_network

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await _run_probe_network(c, "http://router.test", "k", "m", _PROBE,
                                            30, None, ttft_s=10, idle_s=10)
    return asyncio.run(go())


class TestFixESseWithoutDone:
    def test_stream_closed_without_done_keeps_content(self):
        # codex closes the SSE stream with no `data: [DONE]`; old code raised
        # StopAsyncIteration into the generic except -> status 0, content lost.
        body = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n')
        r = _sse_run(body)
        assert r.status_code == 200 and r.content == "Hello"
        assert r.finish_reason == "stop" and not r.error

    def test_empty_stream_is_an_error_not_empty_success(self):
        r = _sse_run(b"")
        assert r.status_code == 0 and "empty stream" in r.error


# MatchResult.ranked rows copied from the run-3 status JSON.
_RANKED_OPUS = [
    {"id": "claude-opus-5-5", "score": 0.856, "dims": {
        "tokenizer": 0.45, "capability_curve": 1.0, "knowledge_bank": 0.972, "discriminator": 1.0}},
    {"id": "claude-fable-5-1", "score": 0.836, "dims": {
        "tokenizer": 0.17, "capability_curve": 1.0, "knowledge_bank": 0.917, "discriminator": 1.0}},
]
_RANKED_GLM = [
    {"id": "glm-5-2", "score": 0.713, "dims": {
        "tokenizer": 1.0, "capability_curve": 0.743, "knowledge_bank": 0.829, "discriminator": 0.7}},
    {"id": "glm-5-3", "score": 0.706, "dims": {
        "tokenizer": 1.0, "capability_curve": 0.735, "knowledge_bank": 0.833, "discriminator": 0.8}},
]


class TestFixFJudgeContradiction:
    def test_run3_opus_judge_flip_to_fable_is_rejected(self):
        from app.authenticity.judge_rank import judge_pick_contradicted
        assert judge_pick_contradicted(_RANKED_OPUS, "claude-fable-5-1", "claude-opus-5-5")

    def test_run3_glm_judge_pick_is_kept(self):
        from app.authenticity.judge_rank import judge_pick_contradicted
        assert not judge_pick_contradicted(_RANKED_GLM, "glm-5-3", "glm-5-2")

    def test_no_shared_decisive_lane_is_not_a_contradiction(self):
        from app.authenticity.judge_rank import judge_pick_contradicted
        ranked = [{"id": "a", "dims": {}}, {"id": "b", "dims": {"tokenizer": 1.0}}]
        assert not judge_pick_contradicted(ranked, "a", "b")


def _fv(conf):
    from types import SimpleNamespace
    return SimpleNamespace(family="claimed", behavior_family="", confidence=conf,
                           surface_family="claimed", injection_detected=False, evidence={})


def _mr(matched, score, jt, best="", ru="", ru_score=0.0, ranked=None):
    from app.authenticity.matcher import MatchResult
    d = {"judge_tiebreak": jt} if jt else {}
    return MatchResult(matched_model=matched, match_score=score, runner_up=ru,
                       runner_up_score=ru_score, abstain=not matched, dimensions=d,
                       best_model=best or matched, ranked=ranked or [])


def _reality(claimed, family, mr, fam_conf):
    from app.authenticity.scoring import compute_reality
    return compute_reality(claimed, "http://127.0.0.1:6969", claimed,
                           {"verdict_scope": "full", "family": family, "focus": "", "reason": ""},
                           _fv(fam_conf), mr, [], [], 1000)


class TestFixGVerdict:
    def test_run3_glm_judge_confirmed_claim_is_real(self):
        # fused 0.5*0.806 + 0.5*0.706 = 0.756 < 0.80 -> was UNVERIFIABLE.
        jt = {"picked": "glm-5-3", "confidence": 0.88, "accepted": True}
        mr = _mr("glm-5-3", 0.706, jt, best="glm-5-2", ru="glm-5-2", ru_score=0.713)
        v = _reality("ZHIPU/GLM-5.3", "glm", mr, 0.806)
        assert v.verdict == "real" and v.identified_as == "glm-5-3"
        assert v.evidence.get("resolved_by") == "judge_tiebreak"
        assert v.closest_candidates[0]["id"] == "glm-5-3"

    def test_unaccepted_judge_record_does_not_confirm(self):
        jt = {"forward": None, "reversed": None, "accepted": False,
              "rejected": "order_inconsistent"}
        mr = _mr("", 0.713, jt, best="glm-5-2", ru="glm-5-3", ru_score=0.706)
        v = _reality("ZHIPU/GLM-5.3", "glm", mr, 0.806)
        assert v.verdict != "real"

    def test_run3_opus_matched_fable_without_hint_is_downgrade(self):
        # focus='' (no Reference hint) skipped the downgrade check -> REAL fable.
        mr = _mr("claude-fable-5-1", 0.836, None)
        v = _reality("mintrouter/claude-opus-5-5", "claude", mr, 0.82)
        assert v.verdict == "downgrade"
        assert v.evidence["claimed_submodel"] == "claude-opus-5-5"

    def test_same_submodel_version_boundaries(self):
        from app.authenticity.scoring import same_submodel
        assert not same_submodel("claude-opus-5", "claude-opus-5-5")
        assert not same_submodel("glm-5-3", "glm-5-3-flash")
        assert same_submodel("vsllm-o/kimi-k3-api", "kimi-k3")
        assert same_submodel("ZHIPU/GLM-5.3", "glm-5-3")
        assert same_submodel("glm-4.6", "glm_glm-4.6")


class TestFixICutoffMonthUnknown:
    def test_month_zero_on_standard_is_not_a_month_mismatch(self):
        base = _std(cutoff=2024)  # month 0, like glm-5-3's real standard
        feats = FingerprintFeatures(capability_correct=3, cutoff_year=2024, cutoff_month=10)
        assert match_submodel(feats, {"m": base}).dimensions["cutoff"] == 0.8  # old: 0.3


# 2026-10-23 audit: cache isolation, evidence grading and native transport pools.
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

import app.main as main
import app.authenticity.gate as gate
import app.authenticity.harvest as harvest
import app.authenticity.probes as probes
import app.authenticity.standard_v2 as v2
from app.authenticity.cache import AuthenticityCache, reset_cache
from app.authenticity.classifier import classify_family
from app.authenticity.fingerprint import extract_features
from app.authenticity.scoring import RealityVerdict, canonical_submodel


@pytest.fixture
def audit_gate(monkeypatch):
    reset_cache()
    features = FingerprintFeatures()
    monkeypatch.setattr(gate, "_load_family_ref", lambda mid: {"family": "claude"})
    monkeypatch.setattr(gate, "_load_families", lambda: {})
    monkeypatch.setattr(gate, "_load_match_baselines", lambda mid: {"std": _std()})
    monkeypatch.setattr("app.authenticity.fingerprint.extract_features", lambda *a: features)
    monkeypatch.setattr(probes, "TIER1_PROBES", [{"id": "tier1"}])
    monkeypatch.setattr(probes, "TIER2_PROBES", [{"id": "tier2"}])
    monkeypatch.setattr(probes, "LOGPROB_PROBES", [])
    monkeypatch.setattr(harvest, "NONCODING_PROBES", [])

    async def probe(*args, **kwargs):
        return SimpleNamespace(status_code=200, content="offline", error="", logprobs=None)

    async def empty(*args, **kwargs):
        return {}

    monkeypatch.setattr(probes, "run_probe", probe)
    for name in ("run_boundary_probes", "run_refusal_probes", "run_stability_probe", "run_timing_probe"):
        monkeypatch.setattr(probes, name, empty)
    monkeypatch.setattr(v2, "run_standard_v2", empty)
    yield features
    reset_cache()


def _audit_check(**kwargs):
    config = {"no_cache": True, "max_tier": 2, "total_timeout_s": 60}
    config.update(kwargs.pop("config", {}))
    return gate.authenticity_check("claude-opus-5-5", "http://offline.test", "test-key",
                                   config, mode="deep", **kwargs)


def test_cache_reference_hint_isolation_and_legacy_keys():
    cache = AuthenticityCache()
    verdict = RealityVerdict(model_id="m", endpoint="x", claimed="m", verdict="family_match")
    cache.put("x", "m", verdict)
    cache.put("x", "m", verdict, reference_hint=" CLAUDE ")
    assert cache._key("x", "m") == "x::m"
    assert cache.get("x", "m", "claude") is verdict
    assert cache.get("x", "m", "gpt") is None
    cache.invalidate("x", "m", "Claude")
    assert cache.get("x", "m", "claude") is None
    assert cache.get("x", "m") is verdict


def test_gate_grades_shuffled_needle_items(monkeypatch, audit_gate):
    import app.authenticity.knowledge_needles as kn
    items = [{"fact": "first event", "needle": "Alpha", "kind": "known"},
             {"fact": "second event", "needle": "Beta", "kind": "known"}]
    monkeypatch.setattr(kn, "collect_family_needles", lambda pool: items)
    monkeypatch.setattr(kn, "build_needle_probe", lambda ns: {"id": "knowledge_needles", "_items": ns[::-1]})

    async def probe(*args, **kwargs):
        return SimpleNamespace(status_code=200, content="1. Beta\n2. Alpha", error="", logprobs=None)

    monkeypatch.setattr(probes, "run_probe", probe)
    asyncio.run(_audit_check())
    assert set(audit_gate.knowledge_needles["known_hits"]) == {"first event", "second event"}


def test_candidate_local_needle_kind_and_needle_text():
    base = {"knowledge": {"known_facts": [{"fact": "event A", "needle": "Alpha"}],
                          "post_cutoff_unknown": [{"fact": "event B", "needle": "Beta"}]}}
    features = _needle_features([("event A", "wrong", "post_cutoff"),
                                 ("event B", "Beta", "known")],
                                {"event A": "Alpha", "event B": "unknown"})
    assert match_submodel(features, {"m": base}).dimensions["knowledge"] == 1.0
    features.knowledge_needles = classify_needle_answers("1. unknown\n2. Beta", [
        {"fact": "event A", "needle": "Alpha", "kind": "post_cutoff"},
        {"fact": "event B", "needle": "Beta", "kind": "known"}])
    assert match_submodel(features, {"m": base}).dimensions["knowledge"] == 0.0


@pytest.mark.parametrize("location", ["structural", "tokenizer_signature", "flat"])
def test_token_jsd_full_standard_unwrapping_and_weights(location):
    from app.authenticity.judge_rank import FEATURE_WEIGHTS, coverage_adjust, decisive_lane_margin
    lp = {f"lp{i}": {"logprob": -0.1, "top": {"A": -0.1, "B": -2.3}} for i in range(4)}
    if location == "structural":
        standard = {"structural": {"logprob_profile": lp}}
    elif location == "tokenizer_signature":
        standard = {"structural": {"tokenizer_signature": {"logprob_profile": lp}}}
    else:
        standard = {"logprob_profile": lp}
    dims = v2.compute_v2_dims({"logprob_profile": lp}, {"m": standard})
    assert dims["m"]["token_jsd"] == 1.0
    assert coverage_adjust({"token_jsd": 1.0}) == {"token_jsd": 1.0}
    ranked = [{"id": "a", "dims": {"token_jsd": 0.2}}, {"id": "b", "dims": {"token_jsd": 0.8}}]
    assert decisive_lane_margin(ranked, "a", "b") == pytest.approx(0.6)
    assert "token_jsd" not in FEATURE_WEIGHTS
    assert sum(FEATURE_WEIGHTS.values()) == pytest.approx(1.0)


def test_gate_passes_live_logprobs_into_v2(monkeypatch, audit_gate):
    audit_gate.logprob_profile = {"lp": {"top": {"A": -0.1}}}

    async def standard(*args, sink, **kwargs):
        sink.update({"tokenizer": {"tokens": 1}})
        return sink

    monkeypatch.setattr(v2, "run_standard_v2", standard)
    asyncio.run(_audit_check())
    assert audit_gate.v2_profile["logprob_profile"] == audit_gate.logprob_profile


@pytest.mark.parametrize("text,month", [
    ("My knowledge cutoff is 2024. I may not know later events.", 0),
    ("My cutoff is May 2024.", 5), ("My cutoff is may of 2024.", 5),
    ("My cutoff is 2024 June.", 6), ("My cutoff is 2024-09.", 9),
    ("My cutoff is 2024/12.", 12), ("My cutoff is 2024-13.", 0),
    ("My knowledge is from 2024, dismay is not a month.", 0)])
def test_cutoff_month_parsing(text, month):
    result = SimpleNamespace(probe_id="cutoff_probe", status_code=200, content=text)
    assert extract_features([result], {}).cutoff_month == month


@pytest.mark.parametrize("name,family", [("kimi-k2.5", "kimi"), ("mimo-v2.5", "mimo"),
    ("qwen2.5", "qwen"), ("solar-model", None), ("vendor-r1-model", "deepseek")])
def test_family_name_priority(name, family):
    from app.authenticity.scope import detect_family
    assert detect_family(name) == family


def test_honest_unknown_identity_is_unverifiable():
    from app.authenticity.scoring import compute_reality
    family = classify_family(FingerprintFeatures(), {})
    assert family.confidence == 0.3 and family.behavior_family == "unknown"
    verdict = compute_reality("claude-opus-5-5", "x", "claude-opus-5-5",
        {"verdict_scope": "full", "family": "claude"}, family, None, [], [], 0)
    assert verdict.verdict == "unverifiable"


@pytest.mark.parametrize("lane", ["needles", "v5", "boundary", "refusal", "v2", "stability", "timing"])
def test_cooperative_cancel_propagates_from_each_deep_lane(monkeypatch, audit_gate, lane):
    import app.authenticity.knowledge_needles as kn
    monkeypatch.setattr(kn, "collect_family_needles", lambda pool: [{"fact": "x", "needle": "x", "kind": "known"}])

    def progress(step, label):
        if step == lane:
            raise RuntimeError("authenticity test cancelled by user")

    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(_audit_check(on_progress=progress))


def test_non_cancel_runtime_error_still_fails_open(monkeypatch, audit_gate):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom-boundary")
    monkeypatch.setattr(probes, "run_boundary_probes", boom)
    assert asyncio.run(_audit_check()).verdict != "error"


def test_reference_hint_fallback_keeps_loader_signatures(monkeypatch, audit_gate):
    family_calls, baseline_calls = [], []
    def family(mid):
        family_calls.append(mid)
        return {"family": "claude"} if mid == "claude" else None
    def baselines(mid):
        baseline_calls.append(mid)
        return {"std": _std()} if mid == "claude" else {}
    monkeypatch.setattr(gate, "_load_family_ref", family)
    monkeypatch.setattr(gate, "_load_match_baselines", baselines)
    verdict = asyncio.run(_audit_check(reference_hint="claude"))
    assert verdict.verdict != "error"
    assert "claude" in family_calls and "claude" in baseline_calls


class _AuditStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def _audit_request(path="/v1internal:generateContent", body=None):
    raw = json.dumps(body or {}).encode()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": path,
        "query_string": b"", "scheme": "http", "server": ("127.0.0.1", 6969),
        "headers": [(b"authorization", b"Bearer test-key")]}, receive)


def test_delete_standard_source_tag_and_slash_route(monkeypatch, tmp_path):
    canonical = tmp_path / harvest.standard_filename("claude", "provider/opus")
    private = tmp_path / harvest.standard_filename("claude", "provider/opus", "local")
    canonical.write_text("{}")
    private.write_text("{}")
    monkeypatch.setattr(harvest, "STANDARDS_DIR", tmp_path)
    response = asyncio.run(main.delete_auth_standard("claude", "provider/opus", source_tag="local"))
    assert response.status_code == 200
    assert canonical.exists() and not private.exists()
    assert not harvest.delete_standard(tmp_path, "claude", "provider/opus", source_tag="local")
    assert any(r.path == "/api/authenticity/standards/{family}/{sub_model:path}" for r in main.app.routes)


def test_router_native_egress_clients_are_isolated(monkeypatch):
    native, router, injected = object(), object(), object()
    clients = iter([native, router])
    monkeypatch.setattr(main, "google_egress_client", None)
    monkeypatch.setattr(main, "_ANTIGRAVITY_EGRESS_CLIENT", None)
    monkeypatch.setattr(main, "_ROUTER_ANTIGRAVITY_EGRESS_CLIENT", None)
    monkeypatch.setattr(main, "build_google_egress_client", lambda: next(clients))
    assert main._get_antigravity_egress_client() is native
    assert main._get_router_antigravity_egress_client() is router
    monkeypatch.setattr(main, "_ANTIGRAVITY_EGRESS_CLIENT", None)
    assert main._get_router_antigravity_egress_client() is router
    monkeypatch.setattr(main, "google_egress_client", injected)
    assert main._get_antigravity_egress_client() is injected
    assert main._get_router_antigravity_egress_client() is injected


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError])
def test_native_retries_stale_pool_once(monkeypatch, stream, error):
    async def go():
        requests = []
        def broken(request):
            requests.append(request)
            raise error("stale connection")
        def healthy(request):
            requests.append(request)
            return httpx.Response(200, stream=_AuditStream(b"native"), headers={"content-type": "application/json"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as old:
            async with httpx.AsyncClient(transport=httpx.MockTransport(healthy)) as fresh:
                monkeypatch.setattr(main, "google_egress_client", None)
                monkeypatch.setattr(main, "_ANTIGRAVITY_EGRESS_CLIENT", old)
                monkeypatch.setattr(main, "build_google_egress_client", lambda: fresh)
                router = object()
                monkeypatch.setattr(main, "_ROUTER_ANTIGRAVITY_EGRESS_CLIENT", router)
                response = await main._forward_antigravity_native(_audit_request(), stream, b"payload")
                assert response.status_code == 200 and len(requests) == 2
                assert requests[0] is not requests[1]
                assert all(r.content == b"payload" for r in requests)
                assert main._get_antigravity_egress_client() is fresh
                assert main._get_router_antigravity_egress_client() is router
                if stream:
                    assert b"".join([chunk async for chunk in response.body_iterator]) == b"native"
                    await response.background()
                else:
                    assert response.body == b"native"
    asyncio.run(go())


@pytest.mark.parametrize("operation", ["control", "catalog"])
def test_control_and_model_catalog_retry_stale_pool(monkeypatch, operation):
    async def go():
        calls = []
        def broken(request):
            calls.append("broken")
            raise httpx.RemoteProtocolError("stale connection")
        def healthy(request):
            calls.append("healthy")
            return httpx.Response(200, stream=_AuditStream(json.dumps(
                {"models": {"native": {"displayName": "Native"}}}).encode()))
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as old:
            async with httpx.AsyncClient(transport=httpx.MockTransport(healthy)) as fresh:
                monkeypatch.setattr(main, "google_egress_client", None)
                monkeypatch.setattr(main, "_ANTIGRAVITY_EGRESS_CLIENT", old)
                monkeypatch.setattr(main, "build_google_egress_client", lambda: fresh)
                monkeypatch.setattr(main, "cs_get_config", lambda: {"antigravity_integration": {
                    "custom_models": [_audit_custom_model()]}})
                request = _audit_request("/v1internal:fetchAvailableModels")
                if operation == "control":
                    response = await main._forward_antigravity_ccpa_control(request, "fetchAvailableModels")
                    payload = json.loads(b"".join([c async for c in response.body_iterator]))
                    await response.background()
                else:
                    response = await main.antigravity_fetch_available_models(request)
                    payload = json.loads(response.body)
                assert response.status_code == 200 and calls == ["broken", "healthy"]
                assert "native" in payload["models"]
    asyncio.run(go())


def test_native_transport_failure_resets_only_native_pool(monkeypatch):
    async def go():
        def broken(request):
            raise httpx.ConnectError("offline")
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            router = object()
            monkeypatch.setattr(main, "google_egress_client", None)
            monkeypatch.setattr(main, "_ANTIGRAVITY_EGRESS_CLIENT", client)
            monkeypatch.setattr(main, "_ROUTER_ANTIGRAVITY_EGRESS_CLIENT", router)
            response = await main._forward_antigravity_native(_audit_request(), False, b"{}")
            assert response.status_code == 502
            assert main._ANTIGRAVITY_EGRESS_CLIENT is None
            assert main._ROUTER_ANTIGRAVITY_EGRESS_CLIENT is router
    asyncio.run(go())


def _audit_custom_model():
    return {"slug": "custom-test", "placeholder_index": 401, "target": "provider/model", "display_name": "Test"}


def test_custom_catalog_bad_entries_do_not_hide_native_models():
    upstream = {"models": {"native": {"displayName": "Native"}}}
    before = copy.deepcopy(upstream)
    models = [None, "bad", {}, {"slug": []}, _audit_custom_model()]
    merged = main._merge_antigravity_custom_models_catalog(upstream, models)
    assert merged["models"]["native"] == upstream["models"]["native"]
    assert "custom-test" in merged["models"]
    assert upstream == before
    malformed_sorts = {**upstream, "agentModelSorts": [{"groups": 42}]}
    assert main._merge_antigravity_custom_models_catalog(malformed_sorts, models) == malformed_sorts
    assert main._merge_antigravity_custom_models_catalog(upstream, 42) == upstream


def test_custom_grpc_catalog_skips_bad_entries():
    native = main._proto_string(1, "models/native") + main._proto_string(2, "Native")
    body = main._proto_varint(10) + main._proto_varint(len(native)) + native
    frame = b"\0" + len(body).to_bytes(4, "big") + body
    result = main._inject_custom_models_into_grpc_web_proto(frame, [None, {}, {"placeholder_index": 402}, _audit_custom_model()])
    assert native in result and b"MODEL_PLACEHOLDER_M401" in result
    assert b"MODEL_PLACEHOLDER_M402" not in result
    assert main._inject_custom_models_into_grpc_web_proto(frame, 42) == frame


def test_cancel_checks_do_not_overwrite_progress(monkeypatch):
    monkeypatch.setattr(main, "_AUTH_TEST_STATUS", {})
    monkeypatch.setattr(main, "_AUTH_TEST_CANCEL", set())
    progress = main._auth_test_progress("m")
    progress("boundary", "Boundary probes")
    saved = dict(main._AUTH_TEST_STATUS["m"])
    progress("_cancel_check", "")
    assert main._AUTH_TEST_STATUS["m"] == saved
    assert saved["status"] == "running"
    main._AUTH_TEST_CANCEL.add("m")
    with pytest.raises(RuntimeError, match="cancelled"):
        progress("_cancel_check", "")


@pytest.mark.parametrize("raw,canonical", [("deepseek_v4_pro", "deepseek-v4-pro"),
    ("glm_glm-4.6", "glm-4-6"), ("qwen_qwen3_max", "qwen3-max"),
    ("claude_claude_opus_5", "claude-opus-5")])
def test_canonical_model_preserves_underscore_versions(raw, canonical):
    assert canonical_submodel(raw) == canonical


def test_capability_score_is_clamped_and_alias_is_not_downgrade():
    features = FingerprintFeatures(capability_correct=3)
    assert match_submodel(features, {"m": _std(cap=1)}).dimensions["capability"] == 1.0
    match = _mr("claude-opus-5", 0.95, None)
    match.alias_group = ["claude-opus-5-5"]
    assert _reality("claude-opus-5-5", "claude", match, 0.95).verdict == "real"


def test_cutoff_override_roundtrip_and_corruption_indices():
    from app.authenticity.traitmark import TraitMark
    from app.authenticity.diffmatrix import detect_corruption
    data = {"meta": {"cutoff_override": "user-stated 2025-01 (self-reported 2024-06)"}}
    assert TraitMark.from_dict(data).to_dict()["meta"]["cutoff_override"] == data["meta"]["cutoff_override"]
    standard = {"knowledge": {"known_facts": [{"fact": "empty", "needle": ""},
        {"fact": "Beta is the answer", "needle": "Beta"}]}}
    assert not any("embedded" in f for f in detect_corruption(standard))

