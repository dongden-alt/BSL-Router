"""Deterministic sub-model matching against baselines (BazaarLink Layer 2)."""
from dataclasses import dataclass


@dataclass
class MatchResult:
    matched_model: str        # best-matching model from baseline
    match_score: float        # 0.0-1.0
    runner_up: str            # second-best model
    runner_up_score: float
    abstain: bool             # True if insufficient discrimination
    dimensions: dict          # per-dimension match details


def match_submodel(
    features,
    baselines: dict,
) -> MatchResult:
    """Match fingerprint features against baseline DB.

    Compares cutoff year/month and capability answers against each
    baseline model. Returns the best match if score >= 0.60 and
    gap to runner-up >= 0.05. Otherwise abstains.

    "Honest unknown" is NOT a fail — a model legitimately may not
    know its own cutoff date. The fail signal is confident hallucination
    of specs inconsistent with the claimed version.
    """
    if not baselines:
        return MatchResult(
            matched_model="", match_score=0.0,
            runner_up="", runner_up_score=0.0,
            abstain=True, dimensions={"error": "no baselines"},
        )

    scores: dict[str, float] = {}
    all_dimensions: dict[str, dict] = {}

    for model_id, baseline in baselines.items():
        score = 0.0
        dims: dict[str, float] = {}

        # Cutoff match (weight: 0.5)
        baseline_year = baseline.get("cutoff_year", 0)
        baseline_month = baseline.get("cutoff_month", 0)
        if features.cutoff_year > 0 and baseline_year > 0:
            if features.cutoff_year == baseline_year:
                if features.cutoff_month == baseline_month:
                    dims["cutoff"] = 1.0
                elif abs(features.cutoff_month - baseline_month) <= 1:
                    dims["cutoff"] = 0.8
                else:
                    dims["cutoff"] = 0.3
            elif abs(features.cutoff_year - baseline_year) == 1:
                dims["cutoff"] = 0.3
            else:
                dims["cutoff"] = 0.0
        else:
            dims["cutoff"] = 0.5  # unknown — neutral

        score += dims["cutoff"] * 0.5

        # Capability match (weight: 0.5)
        baseline_correct = baseline.get("capability_correct", 3)
        if baseline_correct > 0:
            dims["capability"] = features.capability_correct / max(baseline_correct, 1)
        else:
            dims["capability"] = 0.5

        score += dims["capability"] * 0.5

        # NON-CODING (deep layer): family discriminator via judge profile.
        # Compared only when BOTH the suspect and the baseline carry judge
        # scores — otherwise abstain on this dimension (fail-open).
        nc_sus = (getattr(features, "noncoding", {}) or {}).get("judge") or {}
        nc_base = (baseline.get("noncoding") or {}).get("judge") or {}
        axes = ("creative", "eq", "humor", "design")
        shared = [a for a in axes if _nc_score(nc_sus.get(a)) is not None
                  and _nc_score(nc_base.get(a)) is not None]
        if shared:
            sim = sum(1.0 - abs(_nc_score(nc_sus[a]) - _nc_score(nc_base[a])) / 10.0
                      for a in shared) / len(shared)
            dims["noncoding"] = round(sim, 3)
        else:
            dims["noncoding"] = None  # not compared

        # V1 KNOWLEDGE-CUTOFF NEEDLES: the decisive close-version splitter.
        # Compare the suspect's needle answers against THIS baseline's needles.
        kn_sus = getattr(features, "knowledge_needles", {}) or {}
        knowledge = baseline.get("knowledge") or {}
        base_known = knowledge.get("known_facts") or []
        base_post = knowledge.get("post_cutoff_unknown") or []
        if (base_known or base_post) and kn_sus.get("answers"):
            known_hits = set(kn_sus.get("known_hits", []))
            post_claimed = set(kn_sus.get("post_cutoff_claimed", []))
            known_total = len(base_known)
            post_total = len(base_post)
            # recall of facts this version SHOULD know
            recall = (len([f for f in base_known if f in known_hits]) / known_total) if known_total else 1.0
            # wrongly claiming post-cutoff facts = wrong version / hallucinating
            false_know = (len([f for f in base_post if f in post_claimed]) / post_total) if post_total else 0.0
            dims["knowledge"] = round(max(0.0, recall - false_know), 3)
        else:
            dims["knowledge"] = None  # not compared

        # V5 LOGPROB TAIL: weight-level version splitter (LT method). Compare
        # first-token logprobs per shared probe; 0.5 nats = full mismatch scale.
        lp_sus = getattr(features, "logprob_profile", {}) or {}
        lp_base = (((baseline.get("structural") or {}).get("tokenizer_signature") or {})
                   .get("logprob_profile")) or baseline.get("logprob_profile") or {}
        shared_lp = [pid for pid in lp_sus
                     if isinstance(lp_base.get(pid), dict)
                     and lp_base[pid].get("logprob") is not None
                     and isinstance(lp_sus.get(pid), dict)
                     and lp_sus[pid].get("logprob") is not None]
        if shared_lp:
            sim = sum(max(0.0, 1.0 - abs(lp_sus[pid]["logprob"] - lp_base[pid]["logprob"]) / 0.5)
                      for pid in shared_lp) / len(shared_lp)
            dims["logprob"] = round(sim, 3)
        else:
            dims["logprob"] = None  # endpoint hid logprobs / no baseline

        # V6a BOUNDARY SIGNATURE (L1): stop-sequence/EOS + strict JSON-schema
        # compliance. Compared only when BOTH sides carry a signature; never
        # inflates the base score — it only enters the rescue/penalty path via
        # the soft veto below (boundary < 0.5 → abstain).
        b_sus = getattr(features, "boundary_signature", {}) or {}
        b_base = ((baseline.get("structural") or {})
                  .get("tokenizer_signature", {}) or {}).get("boundary_signature") \
            or (baseline.get("structural") or {}).get("boundary_signature") \
            or baseline.get("boundary_signature") or {}
        _b_fields = [f for f in ("stop_strict", "stop_echo", "json_strict")]
        if b_sus and b_base:
            skip_json = (b_sus.get("json_mode") != "supported"
                         or b_base.get("json_mode") != "supported")
            fields = [f for f in _b_fields
                      if not (skip_json and f == "json_strict")]
            if fields:
                sim = sum(1.0 for f in fields if bool(b_sus.get(f)) == bool(b_base.get(f))) / len(fields)
                dims["boundary"] = round(sim, 3)
            else:
                dims["boundary"] = None
        else:
            dims["boundary"] = None  # no suspect data / no baseline

        # V6b STABILITY HASH (L2): temp=0 determinism behavior. Compares the
        # mode hash and distinct-hash count; carries VETO power like logprob.
        st_sus = getattr(features, "stability_profile", {}) or {}
        st_base = ((baseline.get("structural") or {})
                   .get("tokenizer_signature", {}) or {}).get("stability_profile") \
            or (baseline.get("structural") or {}).get("stability_profile") \
            or baseline.get("stability_profile") or {}
        if st_sus and st_base and st_sus.get("mode_hash") and st_base.get("mode_hash"):
            if st_sus.get("mode_hash") == st_base.get("mode_hash"):
                dims["stability"] = 1.0
            elif st_sus.get("distinct_hashes") == st_base.get("distinct_hashes"):
                # same determinism behavior, different content
                dims["stability"] = 0.4
            else:
                dims["stability"] = 0.0
        else:
            dims["stability"] = None  # either side missing/empty

        # V7 TIMING PROFILE (L3): streamed TTFT/chunk-count/chunk-size.
        # Relative tolerance 0.5 per field; score = fraction of matching
        # fields. ADVISORY-ONLY: never forces abstain, never rescues.
        t_sus = getattr(features, "timing_profile", {}) or {}
        t_base = ((baseline.get("structural") or {})
                  .get("tokenizer_signature", {}) or {}).get("timing_profile") \
            or (baseline.get("structural") or {}).get("timing_profile") \
            or baseline.get("timing_profile") or {}
        if t_sus and t_base:
            eps = 1e-6
            matched = 0
            total = 0
            for fld in ("ttft_ms", "chunk_count", "mean_chunk_chars", "total_ms"):
                a, b = t_sus.get(fld), t_base.get(fld)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    total += 1
                    if abs(a - b) / max(abs(b), eps) <= 0.5:
                        matched += 1
            dims["timing"] = round(matched / total, 3) if total else None
        else:
            dims["timing"] = None  # either side missing/empty

        scores[model_id] = score
        all_dimensions[model_id] = dims

    # Rank
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_model, best_score = ranked[0]
    runner_up, runner_up_score = ranked[1] if len(ranked) > 1 else ("", 0.0)

    abstain = best_score < 0.60 or (best_score - runner_up_score) < 0.05

    # Non-coding agreement can rescue a borderline match: a clone matches on
    # code but not on writing/EQ/humor/design. If the noncoding dimension was
    # compared, fold it in as a decisive family-level signal.
    best_dims = all_dimensions.get(best_model, {})
    if best_dims.get("noncoding") is not None:
        nc = best_dims["noncoding"]
        # low non-coding similarity to the claimed baseline is strong FAKE evidence
        if nc < 0.55 and not abstain:
            abstain = True  # code matched but non-coding says different family
        elif nc >= 0.8 and abstain and best_score >= 0.55:
            abstain = False  # non-coding confirms the borderline match
        # expose the non-coding dimension on the result for scoring/conclusions
        best_dims = dict(best_dims)
        best_dims["noncoding"] = nc

    # Logprob tail is decisive: if compared and the first-token distribution
    # differs materially, the weight state is NOT the claimed standard's —
    # no surface signal can override that (search/injection can't fake logits).
    if best_dims.get("logprob") is not None:
        lp = best_dims["logprob"]
        if lp < 0.6 and not abstain:
            abstain = True
        best_dims = dict(best_dims)
        best_dims["logprob"] = lp

    # V6a BOUNDARY soft veto: compared and < 0.5 → the boundary behavior
    # (stop handling, strict JSON compliance) contradicts the baseline's
    # weight-level habits. Same shape as the logprob veto, but softer.
    if best_dims.get("boundary") is not None:
        bd = best_dims["boundary"]
        if bd < 0.5 and not abstain:
            abstain = True
        best_dims = dict(best_dims)
        best_dims["boundary"] = bd

    # V6b STABILITY veto (L2): the temp=0 determinism behavior of the
    # suspect differs from the baseline's (mode hash AND distinct count both
    # differ). Mirrors the logprob veto — replaces V5's decisive role on
    # logprob-hiding upstreams.
    if best_dims.get("stability") is not None:
        st = best_dims["stability"]
        if st < 0.6 and not abstain:
            abstain = True
        best_dims = dict(best_dims)
        best_dims["stability"] = st

    # V7 TIMING is ADVISORY-ONLY: stored in dimensions for the report, but it
    # never forces abstain and never rescues (deliberately no veto branch).

    return MatchResult(
        matched_model=best_model if not abstain else "",
        match_score=best_score,
        runner_up=runner_up,
        runner_up_score=runner_up_score,
        abstain=abstain,
        dimensions=best_dims,
    )


def _nc_score(node) -> float | None:
    """Extract a 0-10 judge score from a noncoding axis node, else None."""
    if not isinstance(node, dict):
        return None
    s = node.get("score")
    try:
        s = float(s)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return max(0.0, min(10.0, s))
