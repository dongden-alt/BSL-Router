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

        scores[model_id] = score
        all_dimensions[model_id] = dims

    # Rank
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_model, best_score = ranked[0]
    runner_up, runner_up_score = ranked[1] if len(ranked) > 1 else ("", 0.0)

    abstain = best_score < 0.60 or (best_score - runner_up_score) < 0.05

    return MatchResult(
        matched_model=best_model if not abstain else "",
        match_score=best_score,
        runner_up=runner_up,
        runner_up_score=runner_up_score,
        abstain=abstain,
        dimensions=all_dimensions.get(best_model, {}),
    )
