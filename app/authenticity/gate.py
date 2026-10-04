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
) -> "RealityVerdict":
    """Main entry point. NEVER raises — fail-open always.

    claimed:        what the seller says this model is (defaults to model_id).
    reference_hint: optional "I think this is X" — narrows which frozen
                    standard to check first. A hypothesis, never ground truth;
                    a wrong hint widens to a broad sweep, never fails-closed.
    """
    from .cache import get_cache
    from .classifier import classify_family
    from .fingerprint import extract_features
    from .matcher import match_submodel
    from .probes import TIER1_PROBES, TIER2_PROBES, run_probe
    from .scoring import RealityVerdict, compute_reality
    from .scope import in_scope

    t0 = time.monotonic()

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

                baselines = _load_baselines(model_id)
                if baselines:
                    match_result = match_submodel(features, baselines)

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
