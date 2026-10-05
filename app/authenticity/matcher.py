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
