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

    def test_determinism_class_mismatch_still_penalised(self):
        base = _std(stability=self._st("h1", distinct=1))
        feats = FingerprintFeatures(capability_correct=3)
        feats.stability_profile = self._st("h2", distinct=3)
        assert match_submodel(feats, {"m": base}).dimensions["stability"] == 0.0


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
