"""Family classification with self-claim zeroing (BazaarLink Layer 0+1)."""
from dataclasses import dataclass


@dataclass
class FamilyVerdict:
    family: str               # predicted family
    confidence: float         # 0.0-1.0
    surface_family: str       # family from raw response ("claimed"/"impostor"/"mixed"/"unknown")
    behavior_family: str      # family from style features (self-claim zeroed)
    injection_detected: bool  # surface says claimed but behavior says another family
    evidence: dict


def classify_family(features, families: dict) -> FamilyVerdict:
    """Classify model family from fingerprint features.

    Uses two independent channels (BazaarLink Layer 0+1):
    1. Surface: raw identity claims (impostor_hits, self_hits)
    2. Behavior: style features — resistant to prompt-layer forgery

    If surface and behavior disagree → prompt injection detected.
    """
    # Surface classification (from identity claims)
    if features.impostor_hits and not features.self_hits:
        surface_family = "impostor"
        surface_confidence = 0.9
    elif features.self_hits and not features.impostor_hits:
        surface_family = "claimed"
        surface_confidence = 0.8
    elif features.impostor_hits and features.self_hits:
        # Mixed signals — likely injection
        surface_family = "mixed"
        surface_confidence = 0.5
    else:
        surface_family = "unknown"
        surface_confidence = 0.3

    # Behavior classification (from style features)
    behavior_family = "unknown"
    behavior_confidence = 0.0

    behavior_scores: dict[str, float] = {}
    for family_name, family_data in families.items():
        score = 0.0
        style = family_data.get("style_baseline", {})
        if not style:
            continue

        # Bullet style match
        if style.get("bullet_style") and features.bullet_style == style["bullet_style"]:
            score += 0.2

        # CJK ratio similarity
        baseline_cjk = style.get("cjk_ratio", 0.0)
        if abs(baseline_cjk - features.cjk_ratio) < 0.03:
            score += 0.3

        # Sentence length similarity
        baseline_asl = style.get("avg_sentence_length", 0)
        if baseline_asl > 0 and features.avg_sentence_length > 0:
            asl_ratio = min(features.avg_sentence_length, baseline_asl) / max(
                features.avg_sentence_length, baseline_asl
            )
            score += asl_ratio * 0.2

        # Header depth
        if style.get("header_depth", 0) == features.header_depth:
            score += 0.1

        # Analogy usage
        if style.get("has_analogy", False) == features.has_analogy:
            score += 0.1

        # Question count similarity
        baseline_qc = style.get("question_count", 0)
        if baseline_qc > 0 and features.question_count > 0:
            qc_ratio = min(features.question_count, baseline_qc) / max(
                features.question_count, baseline_qc
            )
            score += qc_ratio * 0.1

        behavior_scores[family_name] = score

    if behavior_scores:
        best_family = max(behavior_scores, key=behavior_scores.get)
        best_score = behavior_scores[best_family]
        if best_score > 0.4:
            behavior_family = best_family
            behavior_confidence = min(best_score, 1.0)

    # Injection detection: surface claims to be the tested family,
    # but behavior fingerprint points at a DIFFERENT family.
    # behavior_family is a family name like "glm", "kimi", etc.
    # We need the claimed family name to compare against.
    # If surface says "claimed" (self_hits match the model being tested)
    # and behavior says a different family → injection.
    claimed_family_name = ""
    for fname, fdata in families.items():
        if any(kw in " ".join(features.self_hits).lower() for kw in fdata.get("self_keywords", [])):
            claimed_family_name = fname
            break

    injection_detected = (
        surface_family == "claimed"
        and behavior_family not in ("unknown", "claimed")
        and behavior_family != claimed_family_name
        and claimed_family_name != ""
        and behavior_confidence >= 0.5
    )

    # Final verdict
    if surface_family == "impostor":
        final_family = surface_family
        confidence = surface_confidence
    elif injection_detected:
        final_family = "injection"
        confidence = behavior_confidence
    elif behavior_family != "unknown" and behavior_confidence > surface_confidence:
        final_family = "claimed"  # behavior consistent with claim
        confidence = (surface_confidence + behavior_confidence) / 2
    else:
        final_family = surface_family
        confidence = surface_confidence

    return FamilyVerdict(
        family=final_family,
        confidence=confidence,
        surface_family=surface_family,
        behavior_family=behavior_family,
        injection_detected=injection_detected,
        evidence={
            "impostor_hits": list(features.impostor_hits),
            "self_hits": list(features.self_hits),
            "unknown_count": features.unknown_count,
            "claimed_model": features.claimed_model,
            "claimed_org": features.claimed_org,
            "behavior_scores": behavior_scores,
        },
    )
