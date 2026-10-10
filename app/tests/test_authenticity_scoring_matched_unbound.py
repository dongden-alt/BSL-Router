"""Regression tests: compute_reality with match_result=None.

2026-10-07 bug: `matched` was bound only inside `if match_result is not None:`
in app/authenticity/scoring.py. When match_result was None (no baselines on
disk, or Tier 2 skipped in normal mode with max_tier=1) and the fused family
score crossed REAL_THRESHOLD (0.80), the verdict branch referenced the unbound
`matched` → UnboundLocalError, which the gate's fail-open catch wrapped into
an ERROR/UNVERIFIABLE verdict with raw evidence
{"exception": "local variable 'matched' referenced before assignment"}.

Fix: bind `matched = ""` / `sub_score = 0.0` before the match block. Behavior
when match_result is not None is unchanged. These tests pin both paths.
"""
from types import SimpleNamespace

from app.authenticity.scoring import compute_reality


def _fv_stub(conf=0.9):
    """Plain stub family_verdict (spec shape: .family .behavior_family
    .confidence .surface_family .injection_detected .evidence)."""
    return SimpleNamespace(
        family="claimed",
        behavior_family="glm",
        confidence=conf,
        surface_family="glm",
        injection_detected=False,
        evidence={},
    )


def _scope_full():
    return {"verdict_scope": "full", "family": "glm", "focus": "", "reason": ""}


def test_full_scope_high_confidence_match_result_none_real():
    """match_result=None + full scope + family conf 0.9 >= REAL_THRESHOLD:
    must NOT raise; identified_as falls back to claimed or model_id."""
    v = compute_reality(
        model_id="vsllm-o/glm-5.3-max",
        endpoint="http://127.0.0.1:6969",
        claimed="vsllm-o/glm-5.3-max",
        scope_info=_scope_full(),
        family_verdict=_fv_stub(conf=0.9),
        match_result=None,
        modifications=[],
        probe_errors=[],
        latency_ms=40093,
    )
    assert v.verdict == "family_match"  # family-only, NOT real (matcher abstained)
    # identified_as is the confirmed FAMILY, never an echo of the suspect's own
    # claim string (the 2026-10-07 circularity bug).
    assert v.identified_as == "glm"
    assert v.confidence >= 0.80
    # No sub-model evidence exists — matched stays empty, never crashes.
    assert v.submodel_match == ""
    assert v.submodel_score == 0.0


def test_full_scope_mid_confidence_match_result_none_unverifiable():
    """match_result=None + conf 0.5: mid-band (0.30 < total < 0.80) must land
    in the unverifiable else-branch — neither crash nor substitution."""
    v = compute_reality(
        model_id="vsllm-o/glm-5.3-max",
        endpoint="http://127.0.0.1:6969",
        claimed="vsllm-o/glm-5.3-max",
        scope_info=_scope_full(),
        family_verdict=_fv_stub(conf=0.5),
        match_result=None,
        modifications=[],
        probe_errors=[],
        latency_ms=40093,
    )
    assert v.verdict == "unverifiable"
    assert v.confidence == 0.5
