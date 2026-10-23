"""Tests for the V6a/V6b/V7 authenticity cascade lanes.

L1 Boundary Signature, L2 Stability Hash, L3 Timing Profile — matcher
dimensions, gate cascade wiring (deep mode), and harvest capture.

conftest.py already sets BSL_CONFIG_PATH to a session temp config BEFORE
importing app.main, so the live config is never touched (AGENTS.md §7).
All network is mocked: run_probe / stream helpers are monkeypatched —
no router, no upstream, offline tests only.
"""
import asyncio
import json

import httpx
import pytest

import app.authenticity.gate as gate
import app.authenticity.harvest as harvest
import app.authenticity.probes as probes_mod
from app.authenticity.fingerprint import FingerprintFeatures
from app.authenticity.matcher import match_submodel
from app.authenticity.probes import (
    BOUNDARY_PROBES, STABILITY_PROBE, TIMING_PROBE, ProbeResult,
)


# Module-wide: canary-path resolution does real DNS and getaddrinfo stalls
# intermittently exceed the global 10s pytest.ini ceiling (environmental, not
# a regression). The known-good full-suite baseline ran at timeout=60; this
# mark scopes that ceiling to this file only.
pytestmark = pytest.mark.timeout(60)


def _run(coro):
    """Run an async coroutine in a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# ── Fixture builders ─────────────────────────────────────────────────────────

def _baseline(with_new_keys=True, cutoff_year=2026, capability_correct=3,
              boundary=None, stability=None, timing=None):
    """Baseline dict shaped like a frozen standard (TraitMark.to_dict())."""
    base = {
        "cutoff_year": cutoff_year,
        "cutoff_month": 4,
        "capability_correct": capability_correct,
    }
    if with_new_keys:
        base["structural"] = {"tokenizer_signature": {
            "boundary_signature": boundary or {
                "stop_strict": True, "stop_echo": False,
                "finish_reason": "stop", "json_strict": True,
                "json_mode": "supported",
            },
            "stability_profile": stability or {
                "probe_id": "stability_poem_sea", "samples": 3,
                "distinct_hashes": 1,
                "hashes": ["a" * 64] * 3, "mode_hash": "a" * 64,
            },
            "timing_profile": timing or {
                "ttft_ms": 200, "chunk_count": 10,
                "mean_chunk_chars": 20.0, "total_ms": 1000,
            },
        }}
    return base


# ── 1. Matcher: boundary dimension scoring ───────────────────────────────────

class TestBoundaryDimension:
    def test_boundary_match(self):
        feats = FingerprintFeatures()
        feats.boundary_signature = {
            "stop_strict": True, "stop_echo": False, "finish_reason": "stop",
            "json_strict": True, "json_mode": "supported",
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["boundary"] == 1.0

    def test_boundary_mismatch(self):
        feats = FingerprintFeatures()
        feats.boundary_signature = {
            "stop_strict": False, "stop_echo": True, "finish_reason": "length",
            "json_strict": False, "json_mode": "supported",
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["boundary"] == 0.0

    def test_boundary_missing_baseline_is_none(self):
        feats = FingerprintFeatures()
        feats.boundary_signature = {
            "stop_strict": True, "stop_echo": False,
            "json_strict": True, "json_mode": "supported",
        }
        res = match_submodel(feats, {"std": _baseline(with_new_keys=False)})
        assert res.dimensions["boundary"] is None

    def test_boundary_skips_json_when_unsupported(self):
        # json_mode unsupported on either side → only stop fields compared.
        feats = FingerprintFeatures()
        feats.boundary_signature = {
            "stop_strict": True, "stop_echo": False,
            "json_strict": False, "json_mode": "unsupported",
        }
        base = _baseline()
        base["structural"]["tokenizer_signature"]["boundary_signature"]["json_mode"] \
            = "unsupported"
        res = match_submodel(feats, {"std": base})
        # stop_strict + stop_echo both match → 1.0 even though json differs
        assert res.dimensions["boundary"] == 1.0

    def test_boundary_soft_veto_fires_below_0_5(self):
        # Compared AND < 0.5 AND not abstain → abstain = True.
        # Base score must be high enough to avoid natural abstention:
        # cutoff 1.0*0.5 + capability 1.0*0.5 = 1.0.
        feats = FingerprintFeatures()
        feats.cutoff_year = 2026
        feats.cutoff_month = 4
        feats.capability_correct = 3
        feats.boundary_signature = {
            "stop_strict": False, "stop_echo": True,
            "json_strict": False, "json_mode": "supported",
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.abstain is True

    def test_boundary_no_veto_when_already_abstaining(self):
        # Veto only fires when NOT abstaining (mirrors logprob veto shape).
        feats = FingerprintFeatures()
        feats.boundary_signature = {
            "stop_strict": False, "stop_echo": True,
            "json_strict": False, "json_mode": "supported",
        }
        res = match_submodel(feats, {"std": _baseline()})  # base score low → abstain
        assert res.abstain is True
        assert res.dimensions["boundary"] == 0.0


# ── 2. Matcher: stability dimension + veto ───────────────────────────────────

class TestStabilityDimension:
    def test_stability_mode_hash_match(self):
        feats = FingerprintFeatures()
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 1, "hashes": ["a" * 64] * 3,
            "mode_hash": "a" * 64,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["stability"] == 1.0

    def test_stability_same_determinism_different_content(self):
        # distinct_hashes equal (both deterministic) but different content.
        feats = FingerprintFeatures()
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 1, "hashes": ["b" * 64] * 3,
            "mode_hash": "b" * 64,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["stability"] == 0.4

    def test_stability_determinism_class_mismatch_not_compared(self):
        # FIX K (2026-10-13): deterministic standard vs non-deterministic live
        # run is serving-side batching, not weights (genuine glm-5.3 on the
        # SAME provider as its gold standard flipped distinct 1 -> 3). Old: 0.0.
        feats = FingerprintFeatures()
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 3, "hashes": ["b" * 64, "c" * 64, "d" * 64],
            "mode_hash": "b" * 64,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["stability"] is None

    def test_stability_missing_baseline_is_none(self):
        feats = FingerprintFeatures()
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 1, "hashes": ["a" * 64] * 3,
            "mode_hash": "a" * 64,
        }
        res = match_submodel(feats, {"std": _baseline(with_new_keys=False)})
        assert res.dimensions["stability"] is None

    def test_stability_veto_fires_below_0_6(self):
        # 0.4 (same determinism, different content) < 0.6 and not abstain
        # → veto fires. High base score isolates the veto as the cause.
        feats = FingerprintFeatures()
        feats.cutoff_year = 2026
        feats.cutoff_month = 4
        feats.capability_correct = 3
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 1, "hashes": ["b" * 64] * 3,
            "mode_hash": "b" * 64,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["stability"] == 0.4
        assert res.abstain is True

    def test_stability_no_veto_when_already_abstaining(self):
        feats = FingerprintFeatures()
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 3, "hashes": ["b" * 64, "c" * 64, "d" * 64],
            "mode_hash": "b" * 64,
        }
        res = match_submodel(feats, {"std": _baseline()})  # low base → abstain anyway
        assert res.abstain is True
        assert res.dimensions["stability"] is None  # FIX K: class mismatch not compared


# ── 3. Matcher: timing advisory-only ─────────────────────────────────────────

class TestTimingDimension:
    def test_timing_present_in_dimensions(self):
        feats = FingerprintFeatures()
        feats.timing_profile = {
            "ttft_ms": 210, "chunk_count": 11,
            "mean_chunk_chars": 19.0, "total_ms": 1100,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["timing"] == 1.0

    def test_timing_partial_match_within_tolerance(self):
        # ttft 200→290: |290-200|/200 = 0.45 ≤ 0.5 → match.
        # chunk_count 10→16: |16-10|/10 = 0.6 > 0.5 → no match.
        # mean_chunk 20→14: 0.3 → match. total 1000→1450: 0.45 → match.
        feats = FingerprintFeatures()
        feats.timing_profile = {
            "ttft_ms": 290, "chunk_count": 16,
            "mean_chunk_chars": 14.0, "total_ms": 1450,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["timing"] == 0.75

    def test_timing_never_changes_abstain_high_base(self):
        # Timing score 0.0 on a high-base match → still NOT abstaining.
        feats = FingerprintFeatures()
        feats.cutoff_year = 2026
        feats.cutoff_month = 4
        feats.capability_correct = 3
        feats.timing_profile = {
            "ttft_ms": 5000, "chunk_count": 100,
            "mean_chunk_chars": 1.0, "total_ms": 90000,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["timing"] is not None
        assert res.dimensions["timing"] < 0.5
        assert res.abstain is False

    def test_timing_never_rescues_low_base(self):
        # Timing score 1.0 on a naturally-abstaining match → still abstaining.
        feats = FingerprintFeatures()
        feats.timing_profile = {
            "ttft_ms": 200, "chunk_count": 10,
            "mean_chunk_chars": 20.0, "total_ms": 1000,
        }
        res = match_submodel(feats, {"std": _baseline()})
        assert res.dimensions["timing"] == 1.0
        assert res.abstain is True

    def test_timing_missing_baseline_is_none(self):
        feats = FingerprintFeatures()
        feats.timing_profile = {
            "ttft_ms": 200, "chunk_count": 10,
            "mean_chunk_chars": 20.0, "total_ms": 1000,
        }
        res = match_submodel(feats, {"std": _baseline(with_new_keys=False)})
        assert res.dimensions["timing"] is None


# ── 4. Old-standard backward compatibility (regression guard) ────────────────

class TestOldBaselineCompat:
    def test_old_standard_dims_all_none(self):
        # A pre-cascade standard without the new keys → all three dims None.
        feats = FingerprintFeatures()
        feats.boundary_signature = {"stop_strict": True, "json_strict": True}
        feats.stability_profile = {"mode_hash": "a" * 64}
        feats.timing_profile = {"ttft_ms": 100}
        res = match_submodel(feats, {"old": _baseline(with_new_keys=False)})
        assert res.dimensions["boundary"] is None
        assert res.dimensions["stability"] is None
        assert res.dimensions["timing"] is None

    def test_old_standard_score_identical(self):
        # Score path identical to pre-cascade behavior: cutoff+capability only.
        feats = FingerprintFeatures()
        feats.cutoff_year = 2026
        feats.cutoff_month = 4
        feats.capability_correct = 3
        res = match_submodel(feats, {"std": _baseline(with_new_keys=False)})
        assert res.match_score == 1.0
        assert res.abstain is False

    def test_old_standard_no_veto_from_missing_dims(self):
        # None dims must NOT trigger the veto branches.
        feats = FingerprintFeatures()
        feats.cutoff_year = 2020
        feats.capability_correct = 1
        res = match_submodel(feats, {"std": _baseline(with_new_keys=False)})
        # abstain comes from the low base score only — dims stay None.
        assert res.abstain is True
        assert res.dimensions["boundary"] is None
        assert res.dimensions["stability"] is None
        assert res.dimensions["timing"] is None


# ── 5. Probe definition shapes ───────────────────────────────────────────────

class TestProbeShapes:
    def test_boundary_probes_valid(self):
        assert len(BOUNDARY_PROBES) == 2
        for p in BOUNDARY_PROBES:
            assert p["id"]
            assert p["prompt"]
            assert p["max_tokens"] > 0
        assert BOUNDARY_PROBES[0]["id"] == "boundary_stop"
        assert BOUNDARY_PROBES[1]["id"] == "boundary_json"

    def test_stability_probe_valid(self):
        assert STABILITY_PROBE["id"] == "stability_poem_sea"
        assert STABILITY_PROBE["prompt"]
        assert STABILITY_PROBE["max_tokens"] > 0

    def test_timing_probe_valid(self):
        assert TIMING_PROBE["id"]
        assert TIMING_PROBE["prompt"]
        assert TIMING_PROBE["max_tokens"] > 0

    def test_run_probe_accepts_probe_shape(self):
        # run_probe only requires id/prompt/max_tokens — verify no KeyError on
        # the new shapes via a MockTransport round trip (offline).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}]})

        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as client:
                for p in [*BOUNDARY_PROBES, STABILITY_PROBE]:
                    r = await probes_mod.run_probe(
                        client, "http://router.test", "sk-test", "m", p)
                    assert r.status_code == 200
                    assert r.finish_reason == "stop"

        _run(go())


# ── 6. Gate cascade wiring (deep mode) ───────────────────────────────────────

class _FakeMatch:
    """Minimal match_result double with controllable abstain."""

    def __init__(self, abstain=True, score=0.5):
        self.matched_model = "" if abstain else "std"
        self.match_score = score
        self.runner_up = ""
        self.runner_up_score = 0.0
        self.abstain = abstain
        self.dimensions = {}


def _mk_result(content="x", status_code=200, finish_reason="stop"):
    return ProbeResult(probe_id="t", tier=0, status_code=status_code,
                       content=content, thinking="", latency_ms=1,
                       finish_reason=finish_reason)


class TestGateCascade:
    def _run_gate(self, monkeypatch, abstain_after_v5, l2_resolves=False,
                  fail_lane=None, cfg_override=None):
        """Drive authenticity_check's deep cascade with mocked lanes.

        Returns (verdict, calls) where calls records which lanes ran.
        """
        calls = {"boundary": 0, "stability": 0, "timing": 0}

        async def fake_run_probe(client, ep, key, mid, probe, timeout, extra=None):
            return _mk_result()

        async def fake_boundary(client, ep, key, mid, timeout):
            calls["boundary"] += 1
            if fail_lane == "boundary":
                raise RuntimeError("boom-boundary")
            return {"stop_strict": True, "stop_echo": False,
                    "finish_reason": "stop", "json_strict": True,
                    "json_mode": "supported"}

        async def fake_stability(client, ep, key, mid, timeout):
            calls["stability"] += 1
            if fail_lane == "stability":
                raise RuntimeError("boom-stability")
            if l2_resolves:
                return {"probe_id": "stability_poem_sea", "samples": 3,
                        "distinct_hashes": 1, "hashes": ["a" * 64] * 3,
                        "mode_hash": "a" * 64}
            return {"probe_id": "stability_poem_sea", "samples": 3,
                    "distinct_hashes": 3,
                    "hashes": ["b" * 64, "c" * 64, "d" * 64],
                    "mode_hash": "b" * 64}

        async def fake_timing(client, ep, key, mid, timeout):
            calls["timing"] += 1
            if fail_lane == "timing":
                raise RuntimeError("boom-timing")
            return {"ttft_ms": 100, "chunk_count": 5,
                    "mean_chunk_chars": 4.0, "total_ms": 500}

        # In-scope claude model; family ref exists on disk for claude.
        monkeypatch.setattr(gate, "_load_family_ref",
                            lambda mid: {"family": "claude",
                                         "self_keywords": ["claude"],
                                         "impostor_keywords": []})
        monkeypatch.setattr(gate, "_load_families", lambda: {})

        match_state = {"call": 0}

        def fake_match(features, baselines):
            match_state["call"] += 1
            # After L2 lands resolving evidence, stop abstaining.
            if l2_resolves and features.stability_profile:
                return _FakeMatch(abstain=False, score=0.9)
            return _FakeMatch(abstain=abstain_after_v5, score=0.5)

        monkeypatch.setattr("app.authenticity.matcher.match_submodel", fake_match)
        # gate imports matcher lazily inside the function; patch the source too.
        import app.authenticity.matcher as matcher_mod
        monkeypatch.setattr(matcher_mod, "match_submodel", fake_match)
        monkeypatch.setattr(probes_mod, "run_probe", fake_run_probe)
        monkeypatch.setattr(probes_mod, "run_boundary_probes", fake_boundary)
        monkeypatch.setattr(probes_mod, "run_stability_probe", fake_stability)
        monkeypatch.setattr(probes_mod, "run_timing_probe", fake_timing)
        # 2026-10-18 fail-open regression guard: with the v2 canary now
        # fail-open (standard_v2.py), every deep-mode gate run in this file
        # executes the REAL v2 lane battery against http://router.test --
        # each call fails in milliseconds (getaddrinfo) but still pays the
        # 0.75s _pace sleep, stacking 30+ sleeps against the 10s pytest
        # timeout (same hazard documented in test_authenticity_standard_v2).
        # Zero pace here so the genuine fail-open path -- canary 502 ->
        # lanes proceed -> canary_failed_open gap recorded -- stays under
        # test, fast.
        monkeypatch.setattr("app.authenticity.standard_v2._MIN_INTERVAL", 0.0)
        # 2026-10-18 second fix: pace was only half the story. Transport
        # failures (getaddrinfo -> code 0) and 502 are _RETRYABLE, so every
        # failing v2 call re-slept through _RETRY_GAPS=(4.0, 10.0, 25.0) --
        # 39s of backoff PER CALL, which is what actually blew the 10s
        # pytest ceiling (run aborted at 29 dots; BSL log shows canary
        # executed 2x, tok_fp 4x = retries firing, each call failing in
        # 1-14ms). gaps=_RETRY_GAPS is a def-time default arg, so patching
        # the module global is useless -- wrap call_with_backoff instead
        # and force gaps=() : each lane fails once, instantly, and the
        # fail-open verdict (canary_failed_open + per-lane gaps) is still
        # built by the REAL code path. Function-scoped: direct
        # call_with_backoff tests outside _run_gate keep real retries.
        import app.authenticity.standard_v2 as sv2_mod

        _orig_cwb = sv2_mod.call_with_backoff

        async def _no_gap_cwb(*args, **kwargs):
            kwargs.pop("gaps", None)
            return await _orig_cwb(*args, gaps=(), **kwargs)

        monkeypatch.setattr(sv2_mod, "call_with_backoff", _no_gap_cwb)

        cfg = {"cache_ttl_seconds": 0, "total_timeout_s": 90,
               "probe_timeout_s": 5, "max_tier": 2, "v2_min_budget_s": 1}
        cfg.update(cfg_override or {})
        verdict = _run(gate.authenticity_check(
            model_id="claude-opus-5-5", endpoint_base="http://router.test",
            api_key="sk-router", config=cfg, mode="deep"))
        return verdict, calls

    def test_l2_l3_do_not_run_when_not_abstaining(self, monkeypatch):
        verdict, calls = self._run_gate(monkeypatch, abstain_after_v5=False)
        assert calls["boundary"] == 1          # L1 always runs in deep mode
        assert calls["stability"] == 0         # L2 only when abstaining
        assert calls["timing"] == 0            # L3 only when still abstaining

    def test_l2_runs_when_abstaining(self, monkeypatch):
        verdict, calls = self._run_gate(monkeypatch, abstain_after_v5=True,
                                        l2_resolves=True)
        assert calls["boundary"] == 1
        assert calls["stability"] == 1
        # L3 must NOT run: L2 resolved the abstain.
        assert calls["timing"] == 0

    def test_l3_runs_only_when_still_abstaining_after_l2(self, monkeypatch):
        verdict, calls = self._run_gate(monkeypatch, abstain_after_v5=True,
                                        l2_resolves=False)
        assert calls["boundary"] == 1
        assert calls["stability"] == 1
        assert calls["timing"] == 1

    # -- lane_status: a null dimension must never be silent (2026-10-12) --
    # spatial_curve + token_jsd added 2026-10-19, hard_series 2026-10-20: all
    # carry match evidence (FEATURE_WEIGHTS members, DECISIVE_LANES members,
    # emitted by compute_v2_dims), so _V2_LANES includes them and lane_status
    # must classify them like every other lane.
    _ALL_DIMS = {"noncoding", "knowledge", "logprob", "boundary", "stability",
                 "timing", "refusal", "tokenizer", "capability_curve",
                 "knowledge_bank", "discriminator", "spatial_curve",
                 "token_jsd", "hard_series"}

    def test_lane_status_covers_every_dimension_with_a_reason(self, monkeypatch):
        verdict, _ = self._run_gate(monkeypatch, abstain_after_v5=False)
        ls = verdict.evidence["lane_status"]
        assert set(ls) == self._ALL_DIMS
        assert all(isinstance(v, str) and v for v in ls.values())
        assert ls["boundary"] == "ok"
        # Decisive match -> L2/L3 skipped BY DESIGN, and said so.
        assert ls["stability"] == "not_run_match_decisive"
        assert ls["timing"] == "not_run_match_decisive"
        # No claude standard carries logprobs (harvest_gaps) -> permanent.
        assert ls["logprob"] == "no_baseline_data"
        # No judge endpoint wired in this test -> attributable, not bare.
        assert ls["noncoding"].startswith("no_live_data:")
        assert verdict.evidence["evidence_complete"] is False

    def test_lane_status_reports_budget_exhaustion(self, monkeypatch):
        verdict, calls = self._run_gate(monkeypatch, abstain_after_v5=True,
                                        cfg_override={"total_timeout_s": 0})
        ls = verdict.evidence["lane_status"]
        assert calls["boundary"] == 0
        for dim in ("boundary", "refusal", "logprob", "tokenizer", "discriminator"):
            assert ls[dim] == "skipped_budget_exhausted", (dim, ls[dim])
        assert verdict.evidence["evidence_complete"] is False

    def test_lane_exception_swallowed_fail_open(self, monkeypatch):
        # Any lane raising must NOT propagate — gate returns a verdict.
        for lane in ("boundary", "stability", "timing"):
            verdict, calls = self._run_gate(monkeypatch, abstain_after_v5=True,
                                            l2_resolves=False, fail_lane=lane)
            assert verdict is not None
            assert verdict.verdict in ("real", "downgrade", "substitution",
                                       "family_match", "unverifiable", "error")


# ── 7. Harvest capture ───────────────────────────────────────────────────────

def _offline_client_factory():
    """MockTransport client for harvest_model's Tier 1/2 probes (offline)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "offline"},
                         "finish_reason": "stop"}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestHarvestCascadeCapture:
    def test_standards_gain_three_structural_keys(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.authenticity.standard_v2._MIN_INTERVAL", 0.0)
        captured = {}

        async def fake_boundary(client, ep, key, mid, timeout):
            captured["boundary"] = True
            return {"stop_strict": True, "stop_echo": False,
                    "finish_reason": "stop", "json_strict": True,
                    "json_mode": "supported"}

        async def fake_stability(client, ep, key, mid, timeout):
            captured["stability"] = True
            return {"probe_id": "stability_poem_sea", "samples": 3,
                    "distinct_hashes": 1, "hashes": ["a" * 64] * 3,
                    "mode_hash": "a" * 64}

        async def fake_timing(client, ep, key, mid, timeout):
            captured["timing"] = True
            return {"ttft_ms": 100, "chunk_count": 5,
                    "mean_chunk_chars": 4.0, "total_ms": 500}

        # harvest.py binds the lane helpers at MODULE level, so patch there.
        monkeypatch.setattr(harvest, "run_boundary_probes", fake_boundary)
        monkeypatch.setattr(harvest, "run_stability_probe", fake_stability)
        monkeypatch.setattr(harvest, "run_timing_probe", fake_timing)
        monkeypatch.setattr(harvest, "_make_client", _offline_client_factory)
        monkeypatch.setattr(harvest, "STANDARDS_DIR", tmp_path)

        tm = _run(harvest.harvest_model(
            "http://router.test", "sk-router", "claude-opus-5-5",
            include_noncoding=False))

        assert captured == {"boundary": True, "stability": True, "timing": True}
        struct = tm.to_dict()["structural"]["tokenizer_signature"]
        assert struct["boundary_signature"]["stop_strict"] is True
        assert struct["stability_profile"]["mode_hash"] == "a" * 64
        assert struct["timing_profile"]["chunk_count"] == 5
        # V5 logprob capture untouched: mocked anchor returns no logprobs.
        assert tm.to_dict()["structural"]["logprob_profile"] == {}

    def test_harvest_fail_open_when_lanes_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.authenticity.standard_v2._MIN_INTERVAL", 0.0)
        async def boom(*a, **k):
            raise RuntimeError("lane down")

        # harvest.py binds the lane helpers at MODULE level, so patch there.
        monkeypatch.setattr(harvest, "run_boundary_probes", boom)
        monkeypatch.setattr(harvest, "run_stability_probe", boom)
        monkeypatch.setattr(harvest, "run_timing_probe", boom)
        monkeypatch.setattr(harvest, "_make_client", _offline_client_factory)
        monkeypatch.setattr(harvest, "STANDARDS_DIR", tmp_path)

        tm = _run(harvest.harvest_model(
            "http://router.test", "sk-router", "claude-opus-5-5",
            include_noncoding=False))
        # Never raises; gaps recorded for the three missing profiles.
        assert isinstance(tm.meta.harvest_gaps, list)
        gaps = tm.meta.harvest_gaps
        assert any("boundary" in g for g in gaps)
        assert any("stability" in g for g in gaps)
        assert any("timing" in g for g in gaps)

    def test_harvested_standard_roundtrips_through_matcher(self, monkeypatch,
                                                           tmp_path):
        """End-to-end: harvested standard feeds match_submodel's new dims."""
        monkeypatch.setattr("app.authenticity.standard_v2._MIN_INTERVAL", 0.0)
        async def fake_boundary(client, ep, key, mid, timeout):
            return {"stop_strict": True, "stop_echo": False,
                    "finish_reason": "stop", "json_strict": True,
                    "json_mode": "supported"}

        async def fake_stability(client, ep, key, mid, timeout):
            return {"probe_id": "stability_poem_sea", "samples": 3,
                    "distinct_hashes": 1, "hashes": ["a" * 64] * 3,
                    "mode_hash": "a" * 64}

        async def fake_timing(client, ep, key, mid, timeout):
            return {"ttft_ms": 100, "chunk_count": 5,
                    "mean_chunk_chars": 4.0, "total_ms": 500}

        # harvest.py binds the lane helpers at MODULE level, so patch there.
        monkeypatch.setattr(harvest, "run_boundary_probes", fake_boundary)
        monkeypatch.setattr(harvest, "run_stability_probe", fake_stability)
        monkeypatch.setattr(harvest, "run_timing_probe", fake_timing)
        monkeypatch.setattr(harvest, "_make_client", _offline_client_factory)

        tm = _run(harvest.harvest_model(
            "http://router.test", "sk-router", "claude-opus-5-5",
            include_noncoding=False))
        std = tm.to_dict()

        feats = FingerprintFeatures()
        feats.boundary_signature = {
            "stop_strict": True, "stop_echo": False,
            "json_strict": True, "json_mode": "supported"}
        feats.stability_profile = {
            "probe_id": "stability_poem_sea", "samples": 3,
            "distinct_hashes": 1, "hashes": ["a" * 64] * 3,
            "mode_hash": "a" * 64}
        feats.timing_profile = {
            "ttft_ms": 110, "chunk_count": 6,
            "mean_chunk_chars": 4.0, "total_ms": 520}

        res = match_submodel(feats, {"std": std})
        assert res.dimensions["boundary"] == 1.0
        assert res.dimensions["stability"] == 1.0
        assert res.dimensions["timing"] == 1.0
