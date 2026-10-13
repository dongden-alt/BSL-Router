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

