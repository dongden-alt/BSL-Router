"""Main authenticity gate — orchestrates scope, probes, classification, matching.

Reality-based verdict model (see scoring.py): the question is "what model is
this TRULY, and what has been done to the delivery?" — not "is it authentic".

Flow (fail-open always — never raises):
  0. Cache get (fresh stable verdict short-circuits).
  1. scope.in_scope(model_id, reference_hint) → narrows focus. Out-of-scope
     → honest UNVERIFIABLE (never "fake").
  2. Tier 1 probes → features → classify_family (self-claim zeroing).
  3. Impostor → SUBSTITUTION.
  4. Tier 2 probes (cutoff/capability) → merge features → match_submodel
     against frozen baselines.
  5. compute_reality fuses scope + family + sub-model match + modifications.
  6. Cache stable verdicts. Log with verdict.headline().
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only; runtime import stays function-local (fail-open)
    from .scoring import RealityVerdict

logger = logging.getLogger(__name__)

_MODULE_DIR = Path(__file__).parent
_REFERENCE_DIR = _MODULE_DIR / "reference"
_BASELINE_DIR = _REFERENCE_DIR / "baselines"
# v2 lanes that carry match evidence (the "version"/"mode"/"gap" keys do not).
_V2_LANES = ("tokenizer", "capability_curve", "knowledge_bank", "discriminator")
# Every match dimension that only the deep battery can produce.
_DEEP_DIMS = ("noncoding", "knowledge", "logprob", "boundary", "refusal",
              "stability", "timing") + _V2_LANES


async def authenticity_check(
    model_id: str,
    endpoint_base: str,
    api_key: str,
    config: dict,
    claimed: str = "",
    reference_hint: str = "",
    mode: str = "normal",
    judge_endpoint: str = "",
    judge_key: str = "",
    judge_model: str = "",
    on_progress=None,
) -> "RealityVerdict":
    """Main entry point. NEVER raises — fail-open always.

    claimed:        what the seller says this model is (defaults to model_id).
    reference_hint: optional "I think this is X" — narrows which frozen
                    standard to check first. A hypothesis, never ground truth;
                    a wrong hint widens to a broad sweep, never fails-closed.
    mode:           normal | deep | auto. Deep runs the T2.5 non-coding battery
                    (the decisive family discriminator).
    judge_*:        judge model endpoint/key/id (routed through this router).
    """
    from .cache import get_cache
    from .classifier import classify_family
    from .fingerprint import extract_features
    from .matcher import match_submodel
    from .probes import TIER1_PROBES, TIER2_PROBES, run_probe
    from .scoring import RealityVerdict, compute_reality
    from .scope import in_scope

    t0 = time.monotonic()

    # Inject deep-mode + judge config for the T2.5 non-coding lane.
    config = dict(config or {})
    config.setdefault("deep_mode", mode == "deep")
    config.setdefault("mode", mode)
    config.setdefault("judge_endpoint", judge_endpoint)
    config.setdefault("judge_key", judge_key)
    config.setdefault("judge_model", judge_model)

    def _elapsed_ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    def _progress(step: str, label: str):
        try:
            if callable(on_progress):
                on_progress(step, label)
        except RuntimeError:
            raise  # cooperative cancel must propagate, never swallowed
        except Exception:
            pass

    def _check_cancel():
        """Fire the cooperative cancel between probes (on_progress only fires
        at named step transitions, which are too coarse to catch a cancel mid-
        battery)."""
        if callable(on_progress):
            on_progress("_cancel_check", "")

    # Bug 3 fix: derive the tier budget from the requested MODE when the
    # caller didn't pin max_tier. Previously main.py set cfg["max_tier"] but
    # the gate never read it against mode, so auto/deep behaved identically.
    if "max_tier" not in config:
        config["max_tier"] = 2 if str(config.get("mode", "")).lower() == "deep" else 1
    _no_cache = bool(config.get("no_cache"))

    _progress("init", "Loading reference data")

    try:
        # Tier 0: Cache — fresh stable verdict short-circuits re-probing.
        cache_ttl = config.get("cache_ttl_seconds", 1800)
        cache = get_cache(ttl_seconds=cache_ttl)
        cached = None if _no_cache else cache.get(endpoint_base, model_id)
        if cached is not None:
            _progress("done", "cached verdict")
            return cached

        # Scope: how much can we say about this model?
        scope_info = in_scope(model_id, reference_hint)

        # Out of scope → honest UNVERIFIABLE without spending probe budget.
        if scope_info.get("verdict_scope") == "unknown":
            verdict = compute_reality(
                model_id=model_id, endpoint=endpoint_base,
                claimed=claimed or model_id, scope_info=scope_info,
                family_verdict=None, match_result=None, modifications=[],
                probe_errors=[], latency_ms=_elapsed_ms(),
            )
            logger.info("AUTH %s", verdict.headline())
            return verdict

        # Load family reference
        family_ref = _load_family_ref(model_id)
        if family_ref is None:
            verdict = compute_reality(
                model_id=model_id, endpoint=endpoint_base,
                claimed=claimed or model_id,
                scope_info={"verdict_scope": "unknown", "family": scope_info.get("family"),
                            "focus": scope_info.get("focus"),
                            "reason": "no family reference data"},
                family_verdict=None, match_result=None, modifications=[],
                probe_errors=[], latency_ms=_elapsed_ms(),
            )
            logger.info("AUTH %s", verdict.headline())
            return verdict

        # Run probes
        probe_timeout = config.get("probe_timeout_s", 30)
        # Default 300 (was 90): a full deep battery measured 262s wall, so at
        # 90 every budget-guarded lane after Tier 2 (including the v2 standard
        # lanes) was silently skipped -> all v2 match dims null. Config still wins.
        total_timeout = config.get("total_timeout_s", 300)

        # Probe retry/TTFT policy (2026-10-07 wiring): OLD defaults — unset
        # keys give ttft=None / retries=0, the exact legacy scalar path, so
        # production callers that never set these are unchanged.
        _probe_ttft = config.get("probe_ttft_s")   # None when unset -> legacy path
        _probe_idle = config.get("probe_idle_s")
        _probe_retries = int(config.get("probe_retries", 0) or 0)

        def _probe_kwargs(lane_fn) -> dict:
            """Forward the retry/TTFT policy to a probe lane ONLY when that
            lane callable accepts the keywords (run_probe does; the
            boundary/stability/timing wrappers pick them up when probes.py
            grows them). Keeps monkeypatched lane fakes with the older
            signature TypeError-free: unset policy = today's behavior."""
            try:
                import inspect as _inspect
                _params = _inspect.signature(lane_fn).parameters
            except Exception:
                return {}
            _kw = {}
            for _name, _val in (("ttft_s", _probe_ttft),
                                ("idle_s", _probe_idle),
                                ("retries", _probe_retries)):
                if _name in _params:
                    _kw[_name] = _val
            return _kw

        probe_errors: list = []
        family_verdict = None
        match_result = None
        features = None
        # Explicit reason for every lane that did NOT run (2026-10-12). Set at
        # the skip site, so a null dimension is never silent or inferred.
        _lane_skip: dict = {}
        _v2_gap = ""

        def _budget_ok(*lanes: str) -> bool:
            _el = time.monotonic() - t0
            if _el < total_timeout:
                return True
            for _l in lanes:
                _lane_skip.setdefault(_l, "skipped_budget_exhausted")
            logger.warning(
                "AUTH %s: budget exhausted (%.0fs >= total_timeout_s=%s) -> skipping %s",
                model_id, _el, total_timeout, ",".join(lanes))
            return False

        async with httpx.AsyncClient() as client:
            # Tier 1
            _progress("tier1", "Normal · identity/style probes")
            tier1_results = []
            for probe in TIER1_PROBES:
                _check_cancel()
                result = await run_probe(
                    client, endpoint_base, api_key, model_id, probe, probe_timeout,
                    **_probe_kwargs(run_probe),
                )
                result.tier = 1
                tier1_results.append(result)
                if result.error:
                    probe_errors.append(probe["id"])

            # If every Tier 1 probe failed, there is nothing to classify.
            if tier1_results and all(r.error for r in tier1_results):
                verdict = compute_reality(
                    model_id=model_id, endpoint=endpoint_base,
                    claimed=claimed or model_id, scope_info=scope_info,
                    family_verdict=None, match_result=None, modifications=[],
                    probe_errors=probe_errors, latency_ms=_elapsed_ms(),
                )
                logger.warning("AUTH %s", verdict.headline())
                return verdict

            # Extract features + classify
            features = extract_features(tier1_results, family_ref)
            families = _load_families()
            family_verdict = classify_family(features, families)
            _progress("classify", f"Normal · family classified as {family_verdict.family}")

            # Fail fast on impostor → substitution (skip Tier 2)
            if family_verdict.surface_family == "impostor":
                verdict = compute_reality(
                    model_id=model_id, endpoint=endpoint_base,
                    claimed=claimed or model_id, scope_info=scope_info,
                    family_verdict=family_verdict, match_result=None,
                    modifications=[], probe_errors=probe_errors,
                    latency_ms=_elapsed_ms(),
                )
                cache.put(endpoint_base, model_id, verdict)
                logger.warning("AUTH %s — impostor %s",
                               verdict.headline(),
                               family_verdict.evidence.get("impostor_hits"))
                return verdict

            # Tier 2 (if enabled and within time budget)
            max_tier = config.get("max_tier", 2)
            if max_tier >= 2 and _budget_ok("cutoff", "capability"):
                _progress("tier2", "Deep · cutoff/capability probes")
                tier2_results = []
                for probe in TIER2_PROBES:
                    _check_cancel()
                    result = await run_probe(
                        client, endpoint_base, api_key, model_id, probe, probe_timeout,
                        **_probe_kwargs(run_probe),
                    )
                    result.tier = 2
                    tier2_results.append(result)
                    if result.error:
                        probe_errors.append(probe["id"])

                if tier2_results:
                    features2 = extract_features(tier2_results, family_ref)
                    features.cutoff_year = features2.cutoff_year
                    features.cutoff_month = features2.cutoff_month
                    features.capability_correct = features2.capability_correct

                baselines = _load_match_baselines(model_id)
                if baselines:
                    match_result = match_submodel(features, baselines)

            # ── Tier 2.5 NON-CODING (deep mode) — the decisive discriminator ──
            # Coding is the weakest axis; clones match frontier models on code
            # but collapse on writing/EQ/humor/design. Runs only in deep mode,
            # fail-open: any gap leaves features.noncoding empty (matcher then
            # abstains on that dimension instead of blocking).
            _deep = bool(config.get("deep_mode")) or str(config.get("mode", "")).lower() == "deep"
            if not _deep:
                for _l in _DEEP_DIMS:
                    _lane_skip.setdefault(_l, "not_run_normal_mode")
            if _deep and features is not None and _budget_ok(*_DEEP_DIMS):
                _progress("tier25", "Deep · judge grading non-coding traits")
                try:
                    from .harvest import NONCODING_PROBES, judge_scores
                    import re as _re
                    nc_results = []
                    for p in NONCODING_PROBES:
                        _check_cancel()
                        r = await run_probe(client, endpoint_base, api_key, model_id, p, probe_timeout,
                                            **_probe_kwargs(run_probe))
                        r.tier = 3
                        nc_results.append((p, r))
                        if r.error:
                            probe_errors.append(p["id"])
                    nc_outputs = [{"axis": p.get("axis", ""), "id": p["id"],
                                   "content": (r.content or "")[:2000]}
                                  for p, r in nc_results if r.status_code == 200 and r.content]
                    if nc_outputs:
                        joined = " ".join(o["content"] for o in nc_outputs)
                        words = _re.findall(r"[A-Za-z']+", joined)
                        ttr = (len(set(w.lower() for w in words)) / len(words)) if words else 0.0
                        slop = sum(joined.lower().count(w) for w in
                                   ("delve", "tapestry", "landscape", "testament", "navigate",
                                    "crucial", "vibrant", "furthermore", "moreover", "elevate"))
                        nc = {"judge": {}, "probe_count": len(nc_outputs),
                              "slop_score": round(slop / max(len(words), 1), 4),
                              "vocabulary_complexity": round(ttr, 4),
                              "outputs": nc_outputs}
                        _jep = config.get("judge_endpoint", "")
                        _jkey = config.get("judge_key", "")
                        _jmodel = config.get("judge_model", "")
                        if _jep and _jkey and _jmodel:
                            _js = await judge_scores(
                                _jep, _jkey, _jmodel, nc_outputs,
                                timeout_s=int(config.get("judge_timeout_s", 180) or 180))
                            # judge_scores NEVER returns empty: on failure every
                            # axis is {"score": 0, "why": "judge_error"}. Score 0
                            # is "not compared" in the matcher, so keeping it
                            # would hide the failure behind a null dimension.
                            if any(isinstance(v, dict) and (v.get("score") or 0) > 0
                                   for v in (_js or {}).values()):
                                nc["judge"] = _js
                            else:
                                nc["judge_gap"] = f"judge_error:{_jmodel}"
                        else:
                            nc["judge_gap"] = "no_judge_config"
                        if nc.get("judge_gap"):
                            logger.warning("AUTH %s: noncoding judge gap -> %s",
                                           model_id, nc["judge_gap"])
                        features.noncoding = nc
                except RuntimeError:
                    raise  # cooperative cancel: never swallowed by fail-open
                except Exception as _nc_exc:
                    logger.warning("non-coding lane failed (fail-open): %s", _nc_exc)

                # Re-run sub-model match WITH non-coding evidence if we have it.
                if getattr(features, "noncoding", None) and features.noncoding.get("judge"):
                    baselines = _load_match_baselines(model_id)
                    if baselines:
                        match_result = match_submodel(features, baselines)

                # ── V1 KNOWLEDGE-CUTOFF NEEDLES (deep mode) — version splitter ──
                # Needles are drawn from the CANDIDATE STANDARDS (not a fixed
                # battery -> not evadable by a cached benchmark response). Ask
                # the suspect about each dated fact; a real version knows its
                # in-cutoff facts and does NOT know post-cutoff facts.
                if features is not None and _budget_ok("knowledge"):
                    try:
                        from .knowledge_needles import (
                            collect_family_needles, build_needle_probe,
                            classify_needle_answers,
                        )
                        _candidates = _load_match_baselines(model_id)
                        needles = collect_family_needles(_candidates)
                        if needles:
                            _progress("needles", "Deep · knowledge-needle probes")
                            probe = build_needle_probe(needles)
                            r = await run_probe(client, endpoint_base, api_key,
                                                model_id, probe, probe_timeout,
                                                **_probe_kwargs(run_probe))
                            if r.status_code == 200 and r.content:
                                features.knowledge_needles = classify_needle_answers(
                                    r.content, needles)
                                # Re-run match WITH the knowledge dimension.
                                _b2 = _load_match_baselines(model_id)
                                if _b2:
                                    match_result = match_submodel(features, _b2)
                    except Exception as _kn_exc:
                        logger.warning("V1 needle lane failed (fail-open): %s", _kn_exc)

                # ── V5 LOGPROB TAIL (deep mode) — weight-level, search-immune ──
                # First-token logprobs on fixed probes: shifts with a single
                # fine-tune step; retrieval/injection cannot touch the logit
                # head. Skipped silently when the endpoint hides logprobs.
                if features is not None and _budget_ok("logprob"):
                    try:
                        from .probes import LOGPROB_PROBES
                        _progress("v5", "Deep · V5 logprob tail")
                        prof = {}
                        for p in LOGPROB_PROBES:
                            r = await run_probe(
                                client, endpoint_base, api_key, model_id, p,
                                probe_timeout,
                                # stream:False pins the logprob lane to the
                                # scalar body (the streamed lane carries no
                                # logprobs — probes.py contract).
                                extra={"logprobs": True, "top_logprobs": 5,
                                       "temperature": 0, "stream": False},
                                **_probe_kwargs(run_probe),
                            )
                            if r.status_code == 200 and r.logprobs:
                                first = (r.logprobs.get("content") or [{}])[0]
                                prof[p["id"]] = {
                                    "logprob": first.get("logprob"),
                                    "top": {t.get("token"): t.get("logprob")
                                            for t in (first.get("top_logprobs") or [])},
                                }
                        if prof:
                            features.logprob_profile = prof
                            _b3 = _load_match_baselines(model_id)
                            if _b3:
                                match_result = match_submodel(features, _b3)
                    except Exception as _lp_exc:
                        logger.warning("V5 logprob lane failed (fail-open): %s", _lp_exc)

                # ── V6a BOUNDARY lane (L1, deep mode, ALWAYS) ──
                # Stop-sequence/EOS + strict JSON-schema compliance. Closes
                # the V5 gap on logprob-hiding upstreams (0/25 lanes return
                # logprobs on this infrastructure). Fail-open + budget-guard,
                # identical to the lanes above; router-only traffic.
                _l1_landed = False
                if features is not None and _budget_ok("boundary"):
                    try:
                        from .probes import run_boundary_probes
                        _progress("boundary", "Deep · L1 boundary signature")
                        sig = await run_boundary_probes(
                            client, endpoint_base, api_key, model_id, probe_timeout,
                            **_probe_kwargs(run_boundary_probes))
                        # FIX D: land only OBSERVED behavior. The old any-truthy
                        # test dropped a real all-False stop result and would
                        # accept the truthy "error" marker of a failed probe.
                        if sig and (sig.get("stop_mode") == "ok"
                                    or sig.get("json_mode") == "supported"):
                            features.boundary_signature = sig
                            _l1_landed = True
                            _b4 = _load_match_baselines(model_id)
                            if _b4:
                                match_result = match_submodel(features, _b4)
                    except Exception as _bnd_exc:
                        logger.warning("V6a boundary lane failed (fail-open): %s", _bnd_exc)

                # ── REFUSAL/ALIGNMENT lane (deep mode, ALWAYS) ──
                # Neutral public-topic probes; the zh-vs-en refusal delta per axis
                # fingerprints the model FAMILY (not the sub-version). Corroborator
                # only: must not alter identified_as, must not alone produce REAL.
                # Fail-open + budget guard like the lanes above; router-only traffic.
                if features is not None and _budget_ok("refusal"):
                    try:
                        _check_cancel()
                        from .probes import run_refusal_probes
                        _progress("refusal", "Deep · L1 refusal/alignment signature")
                        rsig = await run_refusal_probes(
                            client, endpoint_base, api_key, model_id, probe_timeout,
                            **_probe_kwargs(run_refusal_probes))
                        if rsig and rsig.get("axes"):
                            features.refusal_signature = rsig
                            _b4r = _load_match_baselines(model_id)
                            if _b4r:
                                match_result = match_submodel(features, _b4r)
                    except RuntimeError:
                        raise  # cooperative cancel must propagate
                    except Exception as _ref_exc:
                        logger.warning("refusal lane failed (fail-open): %s", _ref_exc)

                # ── STANDARD v2 lane (deep mode, budget-guarded) ──
                # Tokenizer usage-deltas + capability curve + knowledge bank +
                # discriminator. Judge-free; fresh instances every run so a
                # cache/replay cannot answer them. Fail-open; runs only while
                # total_timeout budget remains.
                #
                # 2026-10-12 fix: v2 carries ~0.47 of the ranking weight and is
                # the ONLY evidence that separates close siblings (opus-5 vs
                # opus-5-5 vs sonnet-5-5). It used to get just the LEFTOVER
                # budget (floor 30s) after needles/logprob/boundary/refusal had
                # spent it, and the outer wait_for discarded every finished lane
                # on timeout -> live runs ended with ALL v2 dims None ->
                # sibling tie -> FAMILY MATCH forever, whatever scoring fix
                # shipped. Now: (1) a guaranteed floor `v2_min_budget_s`
                # (default 150s) even when earlier lanes overran, and (2) a
                # sink dict so lanes that finished before the deadline survive.
                if features is not None:
                    _v2_sink: dict = {}
                    _v2_floor = float(config.get("v2_min_budget_s", 150) or 150)
                    _v2_budget = max(_v2_floor, total_timeout - (time.monotonic() - t0))
                    try:
                        _check_cancel()
                        from .standard_v2 import run_standard_v2
                        _progress("v2", "Deep · v2 tokenizer / capability curve / knowledge bank")
                        await asyncio.wait_for(
                            run_standard_v2(
                                client, endpoint_base, api_key, model_id,
                                probe_timeout, mode="live", sink=_v2_sink),
                            timeout=_v2_budget)
                    except RuntimeError:
                        raise  # cooperative cancel must propagate
                    except Exception as _v2_exc:
                        # str(TimeoutError()) == '' -- log the TYPE so a budget
                        # expiry is never again an empty, undiagnosable line.
                        _v2_gap = f"v2_{type(_v2_exc).__name__}_after_{_v2_budget:.0f}s"
                        logger.warning(
                            "v2 lane failed (fail-open): %s: %s (budget=%.0fs, kept lanes=%s)",
                            type(_v2_exc).__name__, _v2_exc, _v2_budget,
                            [k for k in _V2_LANES if _v2_sink.get(k)])
                    _v2 = _v2_sink
                    if _v2.get("gap"):
                        _v2_gap = ":".join(x for x in (_v2.get("gap"), _v2.get("gap_detail")) if x)
                    if any(_v2.get(k) for k in _V2_LANES):
                        features.v2_profile = _v2
                        _b4v = _load_match_baselines(model_id)
                        if _b4v:
                            match_result = match_submodel(features, _b4v)
                    elif _v2:
                        # Lane returned but carried no usable axis (canary
                        # unreachable or every per-lane budget expired) --
                        # without this the all-None v2 dims downstream are
                        # indistinguishable from a crashed lane.
                        logger.warning(
                            "v2 lane returned no usable lanes (gap=%s); "
                            "v2 dims will be None", _v2_gap or "all_lanes_empty")

                # ── V6b STABILITY lane (L2, deep mode, only if ABSTAINING) ──
                # temp=0 identical-probe repetition; carries veto power
                # (replaces V5's decisive role on logprob-hiding upstreams).
                _l2_landed = False
                if match_result is not None and not match_result.abstain:
                    # By design (cost): L2/L3 only run to break an abstain.
                    for _l in ("stability", "timing"):
                        _lane_skip.setdefault(_l, "not_run_match_decisive")
                if match_result is not None \
                        and match_result.abstain \
                        and features is not None \
                        and _budget_ok("stability"):
                    try:
                        from .probes import run_stability_probe
                        _progress("stability", "Deep · L2 stability profile")
                        prof = await run_stability_probe(
                            client, endpoint_base, api_key, model_id, probe_timeout,
                            **_probe_kwargs(run_stability_probe))
                        if prof and prof.get("mode_hash"):
                            features.stability_profile = prof
                            _l2_landed = True
                            _b5 = _load_match_baselines(model_id)
                            if _b5:
                                match_result = match_submodel(features, _b5)
                    except Exception as _stab_exc:
                        logger.warning("V6b stability lane failed (fail-open): %s", _stab_exc)

                # ── V7 TIMING lane (L3, deep mode, only if STILL abstaining) ──
                # Streamed TTFT/chunk-count/chunk-size. Advisory-only evidence:
                # never vetoes, never rescues. Fail-open always.
                if match_result is not None and not match_result.abstain:
                    _lane_skip.setdefault("timing", "not_run_match_decisive")
                if match_result is not None and match_result.abstain \
                        and features is not None \
                        and _budget_ok("timing"):
                    try:
                        from .probes import run_timing_probe
                        _progress("timing", "Deep · L3 timing profile")
                        prof = await run_timing_probe(
                            client, endpoint_base, api_key, model_id, probe_timeout,
                            **_probe_kwargs(run_timing_probe))
                        if prof and prof.get("chunk_count"):
                            features.timing_profile = prof
                            # Advisory-only: store the profile but do NOT
                            # re-run match_submodel on timing alone — it can
                            # neither veto nor rescue, so re-matching would
                            # spend budget for an identical verdict.
                    except Exception as _tim_exc:
                        logger.warning("V7 timing lane failed (fail-open): %s", _tim_exc)

                # ── JUDGE TIE-BREAK (deep, only on a TRUE tie) ──
                # When the matcher still abstains AND best/runner-up are within
                # the 0.05 discrimination gap, deterministic signals cannot pick
                # a winner. Hand the tied top candidates to the judge (a
                # different family) to pick the closest standard. Overrides the
                # match only when the judge is decisive (confidence >= 0.6).
                # NOT budget-gated (2026-10-12): live run logged
                # `budget exhausted (509s >= 480) -> skipping judge_tiebreak`
                # with a 0.028 gap -- the ONE step that resolves tied siblings
                # was dropped. It is a single judge call already bounded by
                # judge_timeout_s, so it always runs on a true tie.
                if (match_result is not None and match_result.abstain
                        and features is not None):
                    try:
                        _gap = match_result.match_score - match_result.runner_up_score
                        _jep = config.get("judge_endpoint", "")
                        _jkey = config.get("judge_key", "")
                        _jmodel = config.get("judge_model", "")
                        if (_gap < 0.05 and _jep and _jkey and _jmodel
                                and match_result.best_model):
                            _progress("judge_tiebreak", "Deep · judge ranking tied candidates")
                            from .judge_rank import rank_candidates
                            _allb = _load_match_baselines(model_id)
                            _tied = {k: _allb.get(k) for k in
                                     (match_result.best_model, match_result.runner_up)
                                     if k and k in _allb}
                            if _tied:
                                # FIX F (2026-10-13): live run 3 -- the judge
                                # flipped genuine opus-5-5 (0.856) to fable-5-1
                                # (0.836) citing sentence length / header depth,
                                # while fable was WORSE on knowledge_bank (0.917
                                # vs 0.972) and tokenizer (0.17 vs 0.45). Two
                                # guards: (1) ask twice with the candidate order
                                # swapped -- a position-biased pick is not a
                                # pick; (2) reject a pick the deterministic v2
                                # lanes contradict.
                                import asyncio as _aio
                                from .judge_rank import judge_pick_contradicted
                                _jto = int(config.get("judge_timeout_s", 180) or 180)
                                _rev = dict(reversed(list(_tied.items())))
                                if len(_tied) > 1:
                                    _p1, _p2 = await _aio.gather(
                                        rank_candidates(_jep, _jkey, _jmodel, features,
                                                        _tied, timeout_s=_jto),
                                        rank_candidates(_jep, _jkey, _jmodel, features,
                                                        _rev, timeout_s=_jto))
                                else:
                                    _p1 = _p2 = await rank_candidates(
                                        _jep, _jkey, _jmodel, features, _tied,
                                        timeout_s=_jto)
                                _pick = None
                                _jt_rec = {"forward": _p1, "reversed": _p2}
                                if _p1 and _p2 and _p1.get("id") == _p2.get("id"):
                                    _pick = {"id": _p1["id"],
                                             "confidence": min(_p1.get("confidence", 0),
                                                               _p2.get("confidence", 0)),
                                             "why": _p1.get("why", "")}
                                elif _p1 or _p2:
                                    _jt_rec["rejected"] = "order_inconsistent"
                                _ranked_l = list(getattr(match_result, "ranked", None) or [])
                                _other = (match_result.runner_up
                                          if _pick and _pick["id"] == match_result.best_model
                                          else match_result.best_model)
                                if _pick and judge_pick_contradicted(_ranked_l, _pick["id"], _other):
                                    _jt_rec["rejected"] = "contradicted_by_decisive_lanes"
                                    _pick = None
                                # REAL requires total >= REAL_THRESHOLD AND a
                                # matched model. Supplying `matched` on a weak
                                # deterministic score would flip FAKE/abstain to
                                # REAL. Only let a decisive judge break a tie
                                # when the deterministic evidence is already
                                # borderline-strong (>= 0.70).
                                if (_pick and _pick.get("confidence", 0) >= 0.6
                                        and match_result.match_score >= 0.70):
                                    # FIX G: the verdict must carry the PICKED
                                    # model's own score, not the displaced
                                    # leader's.
                                    _sc = {c.get("id"): c.get("score") for c in _ranked_l
                                           if isinstance(c, dict)}
                                    if _pick["id"] != match_result.best_model \
                                            and isinstance(_sc.get(_pick["id"]), (int, float)):
                                        match_result.runner_up = match_result.best_model
                                        match_result.runner_up_score = match_result.match_score
                                        match_result.match_score = float(_sc[_pick["id"]])
                                    match_result.matched_model = _pick["id"]
                                    match_result.abstain = False
                                    match_result.dimensions["judge_tiebreak"] = {
                                        "picked": _pick["id"],
                                        "confidence": _pick["confidence"],
                                        "why": _pick.get("why", ""),
                                        "accepted": True,
                                    }
                                else:
                                    _jt_rec["accepted"] = False
                                    _jt_rec.setdefault(
                                        "rejected", "not_decisive" if (_p1 or _p2) else "no_judge_reply")
                                    match_result.dimensions["judge_tiebreak"] = _jt_rec
                    except Exception as _jt_exc:
                        logger.warning("judge tie-break failed (fail-open): %s", _jt_exc)

        # ── LANE STATUS AUDIT (2026-10-12) ───────────────────────────────
        # A null match dimension must NEVER be silent. Every budget-guarded
        # lane above is skipped WITHOUT a log line when the clock runs out,
        # so a run could report ran_full_battery=true while 10 of 13 match
        # dimensions were null (live opus-5-5, 2026-10-11). Classify every
        # dimension's evidence source so the reason is always on the record:
        #   ok                       -> live evidence landed
        #   no_baseline_data         -> NO candidate standard carries this
        #                               axis, so it can never be compared
        #                               (claude standards: logprob, see
        #                               meta.harvest_gaps logprob:unsupported_by_lane)
        #   skipped_budget_exhausted -> total_timeout_s ran out first
        #   not_run_match_decisive   -> L2/L3 skipped by design (no abstain)
        #   not_run_normal_mode      -> deep-only lane, shallow run
        #   no_live_data[:reason]    -> lane ran but returned nothing usable
        # Skip reasons are RECORDED at the skip site (_lane_skip), not inferred.
        from .standard_v2 import v2_of as _v2_of
        _pool_for_status = _load_match_baselines(model_id) or {}

        def _baseline_has(getter) -> bool:
            for _b in _pool_for_status.values():
                try:
                    if getter(_b):
                        return True
                except Exception:
                    continue
            return False

        def _ts(b: dict, k: str):
            _s = b.get("structural") or {}
            return (_s.get("tokenizer_signature") or {}).get(k) or _s.get(k) or b.get(k)

        _v2_live = getattr(features, "v2_profile", None) or {}
        _lane_sources = {
            "noncoding": (lambda: bool((getattr(features, "noncoding", None) or {}).get("judge")),
                          lambda: _baseline_has(lambda b: b.get("noncoding"))),
            "knowledge": (lambda: bool(getattr(features, "knowledge_needles", None)),
                          lambda: True),
            "logprob": (lambda: bool(getattr(features, "logprob_profile", None)),
                        lambda: _baseline_has(lambda b: _ts(b, "logprob_profile"))),
            "boundary": (lambda: bool(getattr(features, "boundary_signature", None)),
                         lambda: _baseline_has(lambda b: _ts(b, "boundary_signature"))),
            "stability": (lambda: bool(getattr(features, "stability_profile", None)),
                          lambda: _baseline_has(lambda b: _ts(b, "stability_profile"))),
            "timing": (lambda: bool(getattr(features, "timing_profile", None)),
                       lambda: _baseline_has(lambda b: _ts(b, "timing_profile"))),
            "refusal": (lambda: bool(getattr(features, "refusal_signature", None)),
                        lambda: _baseline_has(
                            lambda b: (b.get("refusal") or {}).get("delta_signature"))),
        }
        for _ln in _V2_LANES:
            _lane_sources[_ln] = (
                (lambda k: (lambda: bool(_v2_live.get(k))))(_ln),
                (lambda k: (lambda: _baseline_has(
                    lambda b: (_v2_of(b) or {}).get(k))))(_ln),
            )

        _nc_gap = (getattr(features, "noncoding", None) or {}).get("judge_gap", "")
        _live_reason = {"noncoding": _nc_gap}
        _v2_lane_gaps = (_v2_live.get("lane_gaps") or {}) if isinstance(_v2_live, dict) else {}
        for _ln in _V2_LANES:
            _live_reason[_ln] = (_v2_gap or _v2_lane_gaps.get(_ln)
                                 or ("lane_empty_or_timeout" if _v2_live else ""))

        lane_status: dict = {}
        for _name, (_live_ok, _base_ok) in _lane_sources.items():
            try:
                if _live_ok():
                    lane_status[_name] = "ok"
                elif _name in _lane_skip:
                    lane_status[_name] = _lane_skip[_name]
                elif not _base_ok():
                    lane_status[_name] = "no_baseline_data"
                else:
                    _why = _live_reason.get(_name, "")
                    lane_status[_name] = f"no_live_data:{_why}" if _why else "no_live_data"
            except Exception as _ls_exc:
                lane_status[_name] = f"status_error:{type(_ls_exc).__name__}"

        _missing = {k: v for k, v in lane_status.items() if v != "ok"}
        if _missing:
            logger.warning(
                "AUTH %s: %d/%d evidence lanes produced NO comparable data -> %s",
                model_id, len(_missing), len(lane_status), _missing)

        # Fuse into a reality verdict
        _progress("verdict", "Composing verdict")
        verdict = compute_reality(
            model_id=model_id, endpoint=endpoint_base,
            claimed=claimed or model_id, scope_info=scope_info,
            family_verdict=family_verdict, match_result=match_result,
            modifications=[], probe_errors=probe_errors,
            latency_ms=_elapsed_ms(),
        )
        verdict.evidence["ran_full_battery"] = bool(config.get("max_tier", 1) >= 2)
        # ran_full_battery only means "the deep tier was REQUESTED". These two
        # say whether the evidence actually ARRIVED -- the distinction that let
        # a 10-null result report ran_full_battery=true.
        verdict.evidence["lane_status"] = lane_status
        verdict.evidence["evidence_complete"] = not _missing
        if not _no_cache:
            cache.put(endpoint_base, model_id, verdict)
        _progress("done", verdict.verdict)

        if verdict.verdict in ("substitution", "downgrade"):
            logger.warning("AUTH %s", verdict.headline())
        else:
            logger.info("AUTH %s", verdict.headline())

        return verdict

    except Exception as exc:
        # FAIL-OPEN: any exception → error verdict, let request through.
        logger.warning("authenticity_check failed for %s: %s", model_id, exc)
        return RealityVerdict(
            model_id=model_id, endpoint=endpoint_base,
            claimed=claimed or model_id, verdict="error",
            identified_as="", confidence=0.0,
            evidence={"exception": str(exc)},
            latency_ms=_elapsed_ms(),
        ).finalize()


def _load_family_ref(model_id: str) -> dict | None:
    """Load family reference data for a model ID."""
    ref_path = _REFERENCE_DIR / "families.json"
    if not ref_path.exists():
        return None

    families = json.loads(ref_path.read_text(encoding="utf-8"))
    lower = model_id.lower()

    for family_name, family_data in families.items():
        for kw in family_data.get("self_keywords", []):
            if kw.lower() in lower:
                return family_data

    return None


def _load_families() -> dict:
    """Load all family definitions."""
    ref_path = _REFERENCE_DIR / "families.json"
    if not ref_path.exists():
        return {}
    return json.loads(ref_path.read_text(encoding="utf-8"))


def _load_baselines(model_id: str) -> dict[str, dict]:
    """Load baselines for sub-model matching."""
    from .baseline import BaselineStore

    if not _BASELINE_DIR.exists():
        return {}

    family_ref = _load_family_ref(model_id)
    if family_ref is None:
        return {}

    store = BaselineStore(_BASELINE_DIR)
    family_name = family_ref.get("family", "")
    return store.load_family(family_name)


def _load_match_baselines(model_id: str) -> dict[str, dict]:
    """Baselines for matching = cutoff/capability baselines MERGED with the
    full frozen standards (which carry the noncoding judge profile).

    Standards files are TraitMark dicts; match_submodel reads .get("noncoding")
    for the deep family discriminator and cutoff/capability for version match.
    Keyed consistently with BaselineStore.load_family (family_ prefix stripped).
    """
    from .baseline import BaselineStore

    family_ref = _load_family_ref(model_id)
    if family_ref is None:
        return {}
    family_name = family_ref.get("family", "")
    store = BaselineStore(_BASELINE_DIR)
    merged: dict[str, dict] = {}
    if _BASELINE_DIR.exists():
        # load_family keys keep the "family_" prefix while load_family_standards
        # strips it -- normalize both to the stripped form so each model is ONE
        # candidate carrying both the cutoff/capability baseline AND the frozen
        # standard's v2 lanes. Previously the two key spaces produced parallel
        # duplicate candidates and the baseline-keyed twin (no v2 data) won
        # live matches with every v2 dim None.
        for _k, _v in store.load_family(family_name).items():
            _nk = _k[len(family_name) + 1:] if _k.startswith(family_name + "_") else _k
            merged[_nk] = _v
    standards_dir = _REFERENCE_DIR / "standards"
    for key, std in store.load_family_standards(standards_dir, family_name).items():
        base = merged.get(key, {})
        merged[key] = {**base, **std} if isinstance(std, dict) else std
    return merged
