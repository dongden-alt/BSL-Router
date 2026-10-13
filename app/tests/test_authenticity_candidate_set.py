"""Candidate-set contract tests — authenticity verdict pipeline.

Task (.brain/cc_tasks/authenticity_candidate_set.md, 2026-10-13): live
symptom was codex/gpt-5.6-sol -> UNVERIFIABLE and ZHIPU/GLM-5.3 ->
FAMILY MATCH, unchanged after the E-K fixes. The suspected cause —
gate.py dropping the claimed model's OWN exact standard from the
baselines pool — is REFUTED by these tests: the pool built by
gate._load_match_baselines DOES contain the exact standard next to the
near-name siblings the UI showed as "closest candidate standards".

They pin that contract, then characterize the ACTUAL live-failure
mechanism: the cutoff dimension (matcher.py) scored the exact standard
0.0 for the genuine model's honest self-reported cutoff (2024-06,
recorded in gpt_gpt-5-6-sol.json meta.cutoff_override, which no code
read at the time) against the standard's USER-asserted stored value
(2026-02), while cutoff-less siblings score a neutral 0.5 — so the
exact standard sank below gpt-6-1-sol and the matcher abstained.
FIX L (2026-10-13) wired meta.cutoff_override into the cutoff dim;
this file now also pins the post-fix behavior.
"""
import json
import shutil
from pathlib import Path

from app.authenticity.baseline import BaselineStore
from app.authenticity.classifier import FamilyVerdict
from app.authenticity.fingerprint import FingerprintFeatures
from app.authenticity.gate import _load_match_baselines
from app.authenticity.matcher import match_submodel
from app.authenticity.scoring import compute_reality
from app.authenticity.standard_v2 import v2_of

_STANDARDS = (Path(__file__).resolve().parents[1]
              / "authenticity" / "reference" / "standards")

# The exact canonical standards plus the near-name siblings the live UI
# surfaced as "closest candidate standards".
_GPT_FILES = ("gpt_gpt-5-6-sol.json", "gpt_gpt-6-1-sol.json",
              "gpt_gpt-6-astra.json", "gpt_gpt-6-luna.json")
_GLM_FILES = ("glm_glm-5-3.json", "glm_glm-5-2.json")


def _pool_from(tmp_path: Path, files, family: str) -> dict:
    """Copy the named standards into a scratch dir and load the family pool
    exactly the way gate._load_match_baselines does (via
    BaselineStore.load_family_standards on the standards dir)."""
    for name in files:
        shutil.copy(_STANDARDS / name, tmp_path / name)
    return BaselineStore(tmp_path).load_family_standards(tmp_path, family)


def _std(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sol_features(cutoff_year: int, cutoff_month: int) -> FingerprintFeatures:
    """A suspect shaped like the genuine gpt-5.6-sol: every comparable
    evidence lane cloned from its own frozen standard; only the cutoff
    self-report varies (the live model self-reports 2024-06 — see
    meta.cutoff_override in gpt_gpt-5-6-sol.json)."""
    std = _std(_STANDARDS / "gpt_gpt-5-6-sol.json")
    know = std.get("knowledge") or {}
    f = FingerprintFeatures(
        claimed_model="gpt-5.6-sol",
        cutoff_year=cutoff_year,
        cutoff_month=cutoff_month,
        capability_correct=(know.get("capability_boundary") or {}).get("correct") or 0,
    )
    v2 = v2_of(std)
    if any(v2.get(k) for k in ("tokenizer", "capability_curve",
                               "knowledge_bank", "discriminator")):
        f.v2_profile = v2
    nc = std.get("noncoding") or {}
    f.noncoding = {"judge": {a: v for a, v in nc.items() if isinstance(v, dict)}}
    return f


def _family_verdict(family: str, confidence: float = 0.9) -> FamilyVerdict:
    return FamilyVerdict(family="claimed", confidence=confidence,
                         surface_family="claimed", behavior_family=family,
                         injection_detected=False, evidence={})


def _scope(family: str) -> dict:
    return {"verdict_scope": "full", "family": family, "focus": "",
            "reason": ""}


def _verdict_for(mr, model_id="codex/gpt-5.6-sol", family="gpt"):
    return compute_reality(
        model_id=model_id,
        endpoint="http://127.0.0.1:6969",
        claimed=model_id,
        scope_info=_scope(family),
        family_verdict=_family_verdict(family),
        match_result=mr,
        modifications=[], probe_errors=[], latency_ms=1000)


class TestExactStandardInCandidatePool:
    """The refutation pin: the claimed model's OWN exact standard is ALWAYS
    in the baselines pool built by gate._load_match_baselines (gate.py:828),
    next to the near-name siblings."""

    def test_sol_exact_standard_in_gate_pool(self):
        pool = _load_match_baselines("codex/gpt-5.6-sol")
        assert "gpt-5-6-sol" in pool        # the exact standard
        for sibling in ("gpt-6-1-sol", "gpt-6-astra", "gpt-6-luna"):
            assert sibling in pool          # the near-name siblings

    def test_glm_exact_standard_in_gate_pool(self):
        pool = _load_match_baselines("AI-BOX.VN ZHIPU/GLM-5.3")
        assert "glm-5-3" in pool            # the exact standard
        assert "glm-5-2" in pool

    def test_scratch_pool_contains_exact_and_siblings(self, tmp_path):
        pool = _pool_from(tmp_path, _GPT_FILES, "gpt")
        assert set(pool) == {"gpt-5-6-sol", "gpt-6-1-sol",
                             "gpt-6-astra", "gpt-6-luna"}


class TestGenuineSuspectVerdict:
    """A suspect carrying the exact standard's own evidence must NOT be
    spuriously unverifiable/family_match: the matcher names the exact
    standard decisively and the fused verdict is REAL."""

    def test_standard_shaped_sol_matches_decisively(self, tmp_path):
        pool = _pool_from(tmp_path, _GPT_FILES, "gpt")
        know = _std(_STANDARDS / "gpt_gpt-5-6-sol.json").get("knowledge") or {}
        features = _sol_features(know.get("cutoff_year") or 0,
                                 know.get("cutoff_month") or 0)
        mr = match_submodel(features, pool)
        assert mr.best_model == "gpt-5-6-sol"
        assert mr.abstain is False
        assert mr.matched_model == "gpt-5-6-sol"
        v = _verdict_for(mr)
        assert v.verdict == "real"
        assert v.identified_as == "gpt-5-6-sol"

    def test_glm_standard_shaped_matches_decisively(self, tmp_path):
        pool = _pool_from(tmp_path, _GLM_FILES, "glm")
        std = _std(_STANDARDS / "glm_glm-5-3.json")
        know = std.get("knowledge") or {}
        features = FingerprintFeatures(
            claimed_model="glm-5.3",
            cutoff_year=know.get("cutoff_year") or 0,
            cutoff_month=know.get("cutoff_month") or 0,
            capability_correct=(know.get("capability_boundary") or {}).get("correct") or 0)
        v2 = v2_of(std)
        if any(v2.get(k) for k in ("tokenizer", "capability_curve",
                                   "knowledge_bank", "discriminator")):
            features.v2_profile = v2
        mr = match_submodel(features, pool)
        assert mr.best_model == "glm-5-3"
        assert mr.abstain is False
        v = _verdict_for(mr, model_id="AI-BOX.VN ZHIPU/GLM-5.3", family="glm")
        assert v.verdict == "real"
        assert v.identified_as == "glm-5-3"


class TestSelfReportedCutoffMechanism:
    """Characterization of the ACTUAL live failure (task hypothesis refuted).
    The genuine model self-reports cutoff 2024-06 (recorded in
    gpt_gpt-5-6-sol.json meta.cutoff_override — read by matcher.py since
    FIX L, 2026-10-13), while the frozen standard stores the USER-asserted
    2026-02. Pre-fix the cutoff dim scored the exact standard 0.0 and
    cutoff-less siblings a neutral 0.5, so the exact standard sank below
    gpt-6-1-sol and the matcher abstained — the live UNVERIFIABLE. These
    assertions now pin the POST-fix behavior (the override recovers the
    exact match) plus the no-whitewash guard."""

    def test_self_report_penalty_flips_the_match(self, tmp_path):
        """PRE-FIX characterization (FIX L landed 2026-10-13): before the
        cutoff_override wiring this exact suspect's honest 2024-06 self-report
        scored the exact standard 0.0 on the cutoff dim, dragged it below the
        cutoff-less siblings, and the matcher abstained (live UNVERIFIABLE).
        Updated to assert POST-fix behavior; kept (not deleted) per task spec
        as the regression canary for the original mechanism."""
        pool = _pool_from(tmp_path, _GPT_FILES, "gpt")
        features = _sol_features(2024, 6)   # the honest self-report
        mr = match_submodel(features, pool)
        assert mr.best_model == "gpt-5-6-sol"
        assert mr.abstain is False
        v = _verdict_for(mr)
        assert v.verdict == "real"

    def test_override_recovers_genuine_self_report(self, tmp_path):
        """FIX L canary: the genuine self-report (2024-06, from
        meta.cutoff_override) must score the exact standard's cutoff dim 1.0
        via the override path, making the match decisive and the fused
        verdict REAL."""
        pool = _pool_from(tmp_path, _GPT_FILES, "gpt")
        features = _sol_features(2024, 6)   # the honest self-report
        mr = match_submodel(features, pool)
        assert mr.best_model == "gpt-5-6-sol"
        assert mr.abstain is False
        ranked = {c["id"]: c for c in mr.ranked}
        assert ranked["gpt-5-6-sol"]["dims"]["cutoff"] == 1.0
        v = _verdict_for(mr)
        assert v.verdict == "real"

    def test_override_does_not_whitewash_wrong_cutoff(self, tmp_path):
        """FIX L no-whitewash guard: a suspect cutoff (2020-01) matching
        NEITHER the stored 2026-02 NOR the self-reported 2024-06 must still
        score the exact standard's cutoff dim 0.0 — the override grants no
        free pass to a genuinely wrong date."""
        pool = _pool_from(tmp_path, _GPT_FILES, "gpt")
        features = _sol_features(2020, 1)
        mr = match_submodel(features, pool)
        ranked = {c["id"]: c for c in mr.ranked}
        assert ranked["gpt-5-6-sol"]["dims"]["cutoff"] == 0.0

    def test_unknown_cutoff_recovers_the_exact_match(self, tmp_path):
        """Same suspect, cutoff unknown (model refuses to date itself): the
        neutral 0.5 restores the exact standard's v2 lead and the match
        becomes decisive — proving the cutoff dim alone flips the verdict."""
        pool = _pool_from(tmp_path, _GPT_FILES, "gpt")
        features = _sol_features(0, 0)
        mr = match_submodel(features, pool)
        assert mr.best_model == "gpt-5-6-sol"
        assert mr.abstain is False
