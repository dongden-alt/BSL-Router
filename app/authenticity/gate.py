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
import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_MODULE_DIR = Path(__file__).parent
_REFERENCE_DIR = _MODULE_DIR / "reference"
_BASELINE_DIR = _REFERENCE_DIR / "baselines"


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

    try:
        # Tier 0: Cache — fresh stable verdict short-circuits re-probing.
        cache_ttl = config.get("cache_ttl_seconds", 1800)
        cache = get_cache(ttl_seconds=cache_ttl)
        cached = cache.get(endpoint_base, model_id)
        if cached is not None:
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
        total_timeout = config.get("total_timeout_s", 90)

        probe_errors: list = []
        family_verdict = None
        match_result = None
        features = None

        async with httpx.AsyncClient() as client:
            # Tier 1
            tier1_results = []
            for probe in TIER1_PROBES:
                result = await run_probe(
                    client, endpoint_base, api_key, model_id, probe, probe_timeout,
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
            if max_tier >= 2 and (time.monotonic() - t0) < total_timeout:
                tier2_results = []
                for probe in TIER2_PROBES:
                    result = await run_probe(
                        client, endpoint_base, api_key, model_id, probe, probe_timeout,
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
            if _deep and (time.monotonic() - t0) < total_timeout and features is not None:
                try:
                    from .harvest import NONCODING_PROBES, judge_scores
                    import re as _re
                    nc_results = []
                    for p in NONCODING_PROBES:
                        r = await run_probe(client, endpoint_base, api_key, model_id, p, probe_timeout)
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
                            nc["judge"] = await judge_scores(_jep, _jkey, _jmodel, nc_outputs)
                        else:
                            nc["judge_gap"] = "no_judge_config"
                        features.noncoding = nc
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
                if (time.monotonic() - t0) < total_timeout and features is not None:
                    try:
                        from .knowledge_needles import (
                            collect_family_needles, build_needle_probe,
                            classify_needle_answers,
                        )
                        _candidates = _load_match_baselines(model_id)
                        needles = collect_family_needles(_candidates)
                        if needles:
                            probe = build_needle_probe(needles)
                            r = await run_probe(client, endpoint_base, api_key,
                                                model_id, probe, probe_timeout)
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
                if (time.monotonic() - t0) < total_timeout and features is not None:
                    try:
                        from .probes import LOGPROB_PROBES
                        prof = {}
                        for p in LOGPROB_PROBES:
                            r = await run_probe(
                                client, endpoint_base, api_key, model_id, p,
                                probe_timeout,
                                extra={"logprobs": True, "top_logprobs": 5,
                                       "temperature": 0},
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
                if (time.monotonic() - t0) < total_timeout and features is not None:
                    try:
                        from .probes import run_boundary_probes
                        sig = await run_boundary_probes(
                            client, endpoint_base, api_key, model_id, probe_timeout)
                        if sig and any(v for k, v in sig.items()
                                       if k not in ("finish_reason", "json_mode")):
                            features.boundary_signature = sig
                            _l1_landed = True
                            _b4 = _load_match_baselines(model_id)
                            if _b4:
                                match_result = match_submodel(features, _b4)
                    except Exception as _bnd_exc:
                        logger.warning("V6a boundary lane failed (fail-open): %s", _bnd_exc)

                # ── V6b STABILITY lane (L2, deep mode, only if ABSTAINING) ──
                # temp=0 identical-probe repetition; carries veto power
                # (replaces V5's decisive role on logprob-hiding upstreams).
                _l2_landed = False
                if match_result is not None \
                        and match_result.abstain \
                        and (time.monotonic() - t0) < total_timeout \
                        and features is not None:
                    try:
                        from .probes import run_stability_probe
                        prof = await run_stability_probe(
                            client, endpoint_base, api_key, model_id, probe_timeout)
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
                if match_result is not None and match_result.abstain \
                        and (time.monotonic() - t0) < total_timeout \
                        and features is not None:
                    try:
                        from .probes import run_timing_probe
                        prof = await run_timing_probe(
                            client, endpoint_base, api_key, model_id, probe_timeout)
                        if prof and prof.get("chunk_count"):
                            features.timing_profile = prof
                            # Advisory-only: store the profile but do NOT
                            # re-run match_submodel on timing alone — it can
                            # neither veto nor rescue, so re-matching would
                            # spend budget for an identical verdict.
                    except Exception as _tim_exc:
                        logger.warning("V7 timing lane failed (fail-open): %s", _tim_exc)

        # Fuse into a reality verdict
        verdict = compute_reality(
            model_id=model_id, endpoint=endpoint_base,
            claimed=claimed or model_id, scope_info=scope_info,
            family_verdict=family_verdict, match_result=match_result,
            modifications=[], probe_errors=probe_errors,
            latency_ms=_elapsed_ms(),
        )
        cache.put(endpoint_base, model_id, verdict)

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
        merged.update(store.load_family(family_name))
    standards_dir = _REFERENCE_DIR / "standards"
    for key, std in store.load_family_standards(standards_dir, family_name).items():
        base = merged.get(key, {})
        merged[key] = {**base, **std} if isinstance(std, dict) else std
    return merged
