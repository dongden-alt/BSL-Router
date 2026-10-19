"""Offline tests for app/authenticity/diffmatrix.py — Phase 0/1 discrimination index.

Zero API calls. Uses synthetic standards fixtures for unit tests and the real
standards directory only for the integration tests, which are read-only.
"""
from __future__ import annotations

import pytest

from app.authenticity import diffmatrix as dm


def _make_standard(
    model_id="x-model",
    family="xfam",
    cutoff_year=2024,
    cutoff_month=6,
    known_needles=("fact a needle", "fact b needle"),
    unknown_needles=("future c needle",),
    noncoding=(6, 6, 6, 6),
    cjk_ratio=0.0,
    tokenizer_boundary=None,
    refusal_style="polite refusal with brief apology and redirection",
    style_overrides=None,
    lexical_slop=0.30,
):
    boundary = tokenizer_boundary
    if boundary is None:
        boundary = {"stop_strict": True, "stop_echo": False, "finish_reason": "stop",
                    "json_strict": True, "json_mode": "strict"}
    style = {
        "bullet_style": "-",
        "header_depth": 2,
        "avg_sentence_length": 18.0,
        "sentence_length_variance": 6.0,
        "cjk_ratio": cjk_ratio,
        "emoji_usage": 0.02,
        "markdown_density": 0.35,
        "default_response_length": 400,
        "refusal_style": refusal_style,
    }
    if style_overrides:
        style.update(style_overrides)
    return {
        "meta": {"model_id": model_id, "family": family},
        "identity": {
            "self_claim_model": model_id,
            "self_claim_org": family,
            "refusal_identity": "",
        },
        "knowledge": {
            "cutoff_year": cutoff_year,
            "cutoff_month": cutoff_month,
            "known_facts": [
                {"fact": f"fact text for {n}", "needle": n} for n in known_needles
            ],
            "post_cutoff_unknown": [
                {"fact": f"unknown text for {n}", "needle": n} for n in unknown_needles
            ],
        },
        "structural": {
            "tokenizer_signature": {
                "boundary_signature": boundary,
                "stability_profile": {"mode_hash": f"hash-{family}"},
            }
        },
        "style": style,
        "lexical": {"slop_score": lexical_slop, "vocabulary_complexity": 0.5,
                    "signature_phrases": [], "forbidden_words": []},
        "noncoding": {
            axis: {"score": score, "why": "fixture"}
            for axis, score in zip(("creative", "eq", "humor", "design"), noncoding)
        },
    }


# ---------------------------------------------------------------- unit: comparators

def test_tokenizer_discriminates_on_boundary_diff():
    a = _make_standard(tokenizer_boundary={"stop_strict": True, "finish_reason": "stop"})
    b = _make_standard(model_id="y", family="yfam",
                       tokenizer_boundary={"stop_strict": False, "finish_reason": "length"})
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    res = dm.cmp_tokenizer(fa["tokenizer"], fb["tokenizer"])
    assert res.status == "discriminates"
    assert any("stop_strict" in e for e in res.evidence)


def test_tokenizer_unknown_when_missing():
    a = _make_standard()
    b = _make_standard(model_id="y", family="yfam")
    b["structural"] = {}  # strip tokenizer signature
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    assert dm.cmp_tokenizer(fa["tokenizer"], fb["tokenizer"]).status == "unknown"


def test_cutoff_same_date_different_needles():
    a = _make_standard(known_needles=("alpha event", "beta event"))
    b = _make_standard(model_id="y", family="yfam",
                       known_needles=("gamma event", "delta event"))
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    res = dm.cmp_cutoff(fa["cutoff"], fb["cutoff"])
    assert res.status == "discriminates"
    assert any("jaccard" in e for e in res.evidence)


def test_cutoff_same_date_same_needles_is_similar():
    a = _make_standard()
    b = _make_standard(model_id="y", family="yfam")
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    res = dm.cmp_cutoff(fa["cutoff"], fb["cutoff"])
    assert res.status == "similar"


def test_cultural_discriminates_on_axis_gap():
    a = _make_standard(noncoding=(8, 7, 8, 7))
    b = _make_standard(model_id="y", family="yfam", noncoding=(3, 3, 2, 3))
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    res = dm.cmp_cultural(fa["cultural"], fb["cultural"])
    assert res.status == "discriminates"


def test_formatting_discriminates_on_multi_drift():
    a = _make_standard()
    b = _make_standard(
        model_id="y", family="yfam",
        style_overrides={
            "header_depth": 5,              # drift >= 1.0
            "avg_sentence_length": 45.0,    # drift >= 5.0
            "markdown_density": 0.9,        # drift >= 0.10
            "bullet_style": "*",
        },
    )
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    res = dm.cmp_formatting(fa["formatting"], fb["formatting"])
    assert res.status == "discriminates"
    assert len(res.evidence) >= 2


def test_formatting_single_drift_not_enough():
    a = _make_standard()
    b = _make_standard(model_id="y", family="yfam",
                       style_overrides={"header_depth": 5})  # only ONE drift
    fa, fb = dm.extract_fingerprint(a), dm.extract_fingerprint(b)
    assert dm.cmp_formatting(fa["formatting"], fb["formatting"]).status == "similar"


# ---------------------------------------------------------------- unit: refusal axes reader

def _axes(cn_zh="answer", cn_en="answer", west_en="answer", west_zh="answer",
          ru_en="answer", ru_zh="answer"):
    def cell(verdict):
        return {"verdict": verdict}
    return {
        "cn": {"en": cell(cn_en), "zh": cell(cn_zh)},
        "west": {"en": cell(west_en), "zh": cell(west_zh)},
        "ru": {"en": cell(ru_en), "zh": cell(ru_zh)},
    }


def test_refusal_covered_by_axes_without_legacy_text():
    std = _make_standard(style_overrides={"refusal_style": ""})
    std["refusal"] = {"axes": _axes(cn_zh="soft_deflect"),
                      "delta_signature": {"cn": 0.5}, "templates": {}, "captured": 10}
    fp = dm.extract_fingerprint(std)
    assert fp["refusal"]["covered"] is True


def test_refusal_discriminates_on_verdict_mismatch():
    a = _make_standard(style_overrides={"refusal_style": ""})
    a["refusal"] = {"axes": _axes(cn_zh="soft_deflect", ru_zh="soft_deflect"),
                    "delta_signature": {"cn": 0.5, "ru": 0.5}, "templates": {}}
    b = _make_standard(model_id="y", family="yfam", style_overrides={"refusal_style": ""})
    b["refusal"] = {"axes": _axes(), "delta_signature": {"cn": 0.0, "ru": 0.0},
                    "templates": {}}
    res = dm.cmp_refusal(dm.extract_fingerprint(a)["refusal"],
                         dm.extract_fingerprint(b)["refusal"])
    assert res.status == "discriminates"
    assert any("verdict[cn/zh]" in e for e in res.evidence)
    assert any("delta[cn]" in e for e in res.evidence)


def test_refusal_identical_axes_are_similar():
    a = _make_standard(style_overrides={"refusal_style": ""})
    a["refusal"] = {"axes": _axes(), "delta_signature": {"cn": 0.0}, "templates": {}}
    b = _make_standard(model_id="y", family="yfam", style_overrides={"refusal_style": ""})
    b["refusal"] = {"axes": _axes(), "delta_signature": {"cn": 0.0}, "templates": {}}
    res = dm.cmp_refusal(dm.extract_fingerprint(a)["refusal"],
                         dm.extract_fingerprint(b)["refusal"])
    assert res.status == "similar"


def test_refusal_error_verdicts_are_not_evidence():
    a = _make_standard(style_overrides={"refusal_style": ""})
    a["refusal"] = {"axes": _axes(cn_zh="error"), "delta_signature": {"cn": 0.0},
                    "templates": {}}
    b = _make_standard(model_id="y", family="yfam", style_overrides={"refusal_style": ""})
    b["refusal"] = {"axes": _axes(cn_zh="soft_deflect"),
                    "delta_signature": {"cn": 0.0}, "templates": {}}
    res = dm.cmp_refusal(dm.extract_fingerprint(a)["refusal"],
                         dm.extract_fingerprint(b)["refusal"])
    assert res.status == "similar"
    assert not any("verdict" in e for e in res.evidence)


# ---------------------------------------------------------------- unit: pair types + weights

def test_pair_type_classification():
    assert dm.classify_pair("gpt", "gpt") == dm.PAIR_TYPE_SAME_FAMILY
    assert dm.classify_pair("deepseek", "gpt") == dm.PAIR_TYPE_DISTILLATION_SUSPECT
    assert dm.classify_pair("gpt", "qwen") == dm.PAIR_TYPE_DISTILLATION_SUSPECT
    assert dm.classify_pair("claude", "gemini") == dm.PAIR_TYPE_CROSS_FAMILY


def test_same_family_weights_zero_tokenizer():
    # Same-family pair differing only in tokenizer signature must NOT be
    # separable on that lane alone (weight 0.0) — it needs cutoff/formatting.
    a = _make_standard(model_id="gpt-5.6-sol", family="gpt",
                       cutoff_year=2024, cutoff_month=6,
                       tokenizer_boundary={"stop_strict": True})
    b = _make_standard(model_id="gpt-6-1-sol", family="gpt",
                       cutoff_year=2024, cutoff_month=6,
                       known_needles=("gamma", "delta"),
                       unknown_needles=("zeta",),
                       tokenizer_boundary={"stop_strict": False},
                       refusal_style="polite refusal with brief apology and redirection")
    # Same cutoff date but disjoint needle sets -> cutoff lane discriminates (weight 1.0)
    res = dm.compare_pair(a, b)
    assert res["pair_type"] == dm.PAIR_TYPE_SAME_FAMILY
    assert res["lanes"]["cutoff"]["status"] == "discriminates"
    assert res["separability"] == "separable"


def test_identical_standards_are_inseparable():
    a = _make_standard(model_id="dup-a", family="gpt")
    b = _make_standard(model_id="dup-b", family="gpt")
    res = dm.compare_pair(a, b)
    assert res["separability_score"] == 0.0
    assert res["separability"] == "inseparable_with_current_data"


# ---------------------------------------------------------------- unit: corruption

def test_corruption_detects_concatenation_artifact():
    std = _make_standard()
    std["knowledge"]["post_cutoff_unknown"][0]["fact"] = (
        "Chang'e 6 landed on far side.34 felony counts, May 30, 2024 ||| "
        "Donald Trump was convicted on 34 felony counts."
    )
    flags = dm.detect_corruption(std)
    assert any("|||" in f for f in flags)


def test_corruption_detects_cross_contamination():
    # detector is per-section: a needle from entry j of the SAME section
    # embedded in the fact text of entry i -> contamination flag
    std = _make_standard(
        known_needles=("sora 2 synchronized audio", "gpt realtime api ga"),
    )
    std["knowledge"]["known_facts"][0]["fact"] += (
        ". Separately, gpt realtime api ga was also announced."
    )
    flags = dm.detect_corruption(std)
    assert any("embedded" in f for f in flags)


def test_corruption_detects_unreliable_self_claim():
    std = _make_standard(model_id="gpt-5.6-sol", family="gpt")
    std["identity"]["self_claim_model"] = "OpenAI o3"
    flags = dm.detect_corruption(std)
    assert any("self_claim_model" in f and "unreliable" in f for f in flags)


def test_clean_standard_has_no_flags():
    assert dm.detect_corruption(_make_standard()) == []


# ---------------------------------------------------------------- integration: real standards (read-only)

def test_real_standards_build_index():
    index = dm.build_index()
    # CI FIX (2026-10-19): the public repo's tracked corpus carries 18 canonical
    # standards (privacy split b129e71 -- provider-tagged files are gitignored
    # and stay local-only). The old >= 50 floor was calibrated against the local
    # dev tree (60 files incl. private) and was red in CI from birth (run 72,
    # both legs, assert 18 >= 50). 15 = the 18-file public corpus with headroom
    # for legitimate removals; it still catches catastrophic corpus loss.
    # Canonical-models gates below remain untouched -- they pass on 18.
    assert index["standards_count"] >= 15
    for lane in dm.LANES:
        assert index["lane_coverage"][lane] >= 0
    assert len(index["pairs"]) == index["standards_count"] * (index["standards_count"] - 1) // 2


def test_canonical_models_resolve():
    standards = dm.load_standards()
    ids = sorted(standards)
    for canonical in dm.CANONICAL_MODELS:
        assert dm._resolve_canonical_id(canonical, ids) is not None, (
            f"canonical model {canonical} has no standard on disk"
        )


def test_canonical_models_offline_separable():
    """Phase 1 gate: each canonical model must be separable from every other
    standard using only harvested data already on disk.

    If this fails, the failure output names the exact inseparable pairs and
    lane statuses — that is the finding list, not a reason to weaken the gate.

    Unlocked 2026-10-13: Phase 2 targeted backfill closed the last gap —
    refusal + tokenizer lanes for antigravity/gemini-3.6-flash-high were
    backfilled via the registered same-base sibling lane
    mintrouter/gemini-3.8-flash (the meta.model_id is unregistered; probing it
    would hit the router broadcast quota-burn bug). Verified: all 3 canonical
    models separable from 58/58 with min scores 1.1 / 1.2 / 1.6 >= 1.0.
    """
    index = dm.build_index()
    failures = []
    for report in index["canonical_validation"]:
        if report.get("resolved") is None:
            failures.append(f"{report['canonical']}: standard not found")
            continue
        for ip in report["inseparable_pairs"]:
            failures.append(
                f"{report['canonical']} vs {ip['vs']} [{ip['pair_type']}] "
                f"score={ip['score']} lanes={ip['lane_status']}"
            )
    assert not failures, "canonical separability gaps:\n" + "\n".join(failures)
