"""Verdict taxonomy + weighted fusion.

Reality-based scheme (NOT authentic/inauthentic). The question is:
  "What model is this TRULY, and what has been done to the delivery?"

Verdicts:
  real          — matches the frozen official standard for its (hinted) model
  downgrade     — real family, but a LOWER sub-version than claimed
  substitution  — actually a DIFFERENT family than claimed
  family_match  — right family, sub-model UNVERIFIABLE (no standard on disk)
  unverifiable  — not in scope / no standard → honest unknown, never "fake"
  error         — probes failed (endpoint down, timeout, etc.)

Plus a modifications[] list — what the seller did to the delivery
(prompt injection, output cap, context truncation, thinking strip...).

Confidence is mapped to a 5-level badge, never shown as a raw %.
"""
import datetime
from dataclasses import dataclass, field


# ── Confidence badge levels (5-point, no percentages) ─────────────────────
CONFIDENCE_LEVELS = ["very_low", "low", "medium", "high", "very_high"]


def confidence_level(score: float) -> str:
    """Map a 0-1 numeric score to a 5-level badge."""
    if score < 0.30:
        return "very_low"
    if score < 0.55:
        return "low"
    if score < 0.75:
        return "medium"
    if score < 0.90:
        return "high"
    return "very_high"


@dataclass
class Modification:
    """One detected delivery modification by the seller/provider."""
    type: str                 # system_prompt_injection | output_cap |
                              # context_truncation | thinking_stripped | ...
    detail: str = ""
    claimed: object = None
    measured: object = None

    def to_dict(self):
        d = {"type": self.type, "detail": self.detail}
        if self.claimed is not None:
            d["claimed"] = self.claimed
        if self.measured is not None:
            d["measured"] = self.measured
        return d


@dataclass
class RealityVerdict:
    model_id: str
    endpoint: str
    claimed: str = ""
    verdict: str = "unverifiable"   # real|downgrade|substitution|family_match|
                                    # unverifiable|error
    identified_as: str = ""          # the model it actually appears to be
    confidence: float = 0.0
    confidence_level: str = "very_low"
    family: str = ""                 # detected/confirmed family
    focus: str = ""                  # narrowed sub-model hint (if any)
    modifications: list = field(default_factory=list)  # list[Modification]
    evidence: dict = field(default_factory=dict)
    latency_ms: int = 0
    timestamp: str = ""

    # internal detail retained for the layered report
    behavior_family: str = ""
    surface_family: str = ""
    submodel_match: str = ""
    submodel_score: float = 0.0

    def finalize(self):
        self.confidence_level = confidence_level(self.confidence)
        self.timestamp = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        return self

    def headline(self) -> str:
        """One-line summary for the report header."""
        icons = {
            "real": "✅", "downgrade": "🔻", "substitution": "🔀",
            "family_match": "🟡", "unverifiable": "❓", "error": "⚠️",
        }
        icon = icons.get(self.verdict, "❓")
        mods = f" · {len(self.modifications)} modification(s)" if self.modifications else ""
        return (f"{icon} {self.verdict.upper().replace('_', ' ')} — "
                f"{self.identified_as or self.model_id} "
                f"(confidence: {self.confidence_level.replace('_', ' ')}){mods}")

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "endpoint": self.endpoint,
            "claimed": self.claimed,
            "verdict": self.verdict,
            "identified_as": self.identified_as,
            "confidence": round(self.confidence, 3),
            "confidence_level": self.confidence_level,
            "family": self.family,
            "focus": self.focus,
            "modifications": [m.to_dict() for m in self.modifications],
            "evidence": self.evidence,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
            "headline": self.headline(),
        }


# ── Weights ────────────────────────────────────────────────────────────────
FAMILY_WEIGHT = 0.5
SUBMODEL_WEIGHT = 0.5

REAL_THRESHOLD = 0.80
SUBSTITUTION_THRESHOLD = 0.30


def compute_reality(
    model_id: str,
    endpoint: str,
    claimed: str,
    scope_info: dict,
    family_verdict,
    match_result,
    modifications: list,
    probe_errors: list,
    latency_ms: int,
) -> RealityVerdict:
    """Fuse scope + family + sub-model match + modifications into a verdict.

    family_verdict: object with .surface_family .behavior_family .confidence
                    .injection_detected .evidence
    match_result:   object with .matched_model .match_score .abstain (or None)
    modifications:  list[Modification]
    """
    v = RealityVerdict(
        model_id=model_id,
        endpoint=endpoint,
        claimed=claimed,
        family=scope_info.get("family") or "",
        focus=scope_info.get("focus") or "",
        modifications=list(modifications or []),
        latency_ms=latency_ms,
        surface_family=getattr(family_verdict, "surface_family", ""),
        behavior_family=getattr(family_verdict, "behavior_family", ""),
    )

    # Hard error: nothing probed
    if probe_errors and not getattr(family_verdict, "evidence", None):
        v.verdict = "error"
        v.evidence["errors"] = list(probe_errors)
        return v.finalize()

    # Out of scope → honest unverifiable (never "fake")
    if scope_info.get("verdict_scope") == "unknown":
        v.verdict = "unverifiable"
        v.evidence["reason"] = scope_info.get("reason", "out of scope")
        return v.finalize()

    # Impostor keywords → substitution (different family detected)
    if getattr(family_verdict, "surface_family", "") == "impostor":
        v.verdict = "substitution"
        v.identified_as = getattr(family_verdict, "behavior_family", "unknown")
        v.confidence = getattr(family_verdict, "confidence", 0.5)
        v.evidence["impostor_hits"] = getattr(
            family_verdict, "evidence", {}).get("impostor_hits", [])
        return v.finalize()

    # Compute fused score
    fam_score = getattr(family_verdict, "confidence", 0.0) \
        if getattr(family_verdict, "family", "") in ("claimed", scope_info.get("family")) \
        else getattr(family_verdict, "confidence", 0.0)
    sub_score = 0.0
    matched = ""
    if match_result and not getattr(match_result, "abstain", True):
        sub_score = getattr(match_result, "match_score", 0.0)
        matched = getattr(match_result, "matched_model", "")
        v.submodel_match = matched
        v.submodel_score = sub_score

    if scope_info.get("verdict_scope") == "full" and match_result and not getattr(match_result, "abstain", True):
        total = FAMILY_WEIGHT * fam_score + SUBMODEL_WEIGHT * sub_score
    else:
        total = fam_score  # family-only path

    v.confidence = total

    # Decide verdict
    claimed_family = scope_info.get("family", "")
    behavior_family = getattr(family_verdict, "behavior_family", "")

    if behavior_family and behavior_family not in ("unknown", "claimed") \
            and claimed_family and behavior_family != claimed_family \
            and getattr(family_verdict, "confidence", 0) >= 0.5:
        # Behavior clearly points at a DIFFERENT family → substitution
        v.verdict = "substitution"
        v.identified_as = behavior_family
        _fev = getattr(family_verdict, "evidence", {}) or {}
        if _fev:
            v.evidence["family_classifier"] = _fev
    elif total >= REAL_THRESHOLD:
        # Matches the (hinted) standard. Check for downgrade: matched a
        # different sub-model than the claimed/focus one.
        v.identified_as = matched or claimed or model_id
        focus = scope_info.get("focus", "")
        if focus and matched and focus not in matched and matched not in focus:
            v.verdict = "downgrade"
            v.identified_as = matched
        else:
            v.verdict = "real"
    elif scope_info.get("verdict_scope") == "family":
        v.verdict = "family_match"
        v.identified_as = behavior_family or claimed_family
    elif total <= SUBSTITUTION_THRESHOLD:
        v.verdict = "substitution"
        v.identified_as = behavior_family or "unknown"
        _fev = getattr(family_verdict, "evidence", {}) or {}
        if _fev:
            v.evidence["family_classifier"] = _fev
    else:
        v.verdict = "unverifiable"
        v.evidence["reason"] = "ambiguous — consider Deep mode"

    # Prompt-injection is a MODIFICATION, not a fake verdict
    if getattr(family_verdict, "injection_detected", False):
        v.modifications.append(Modification(
            type="system_prompt_injection",
            detail="surface self-claim disagrees with behavioral fingerprint",
        ))

    if probe_errors:
        v.evidence["probe_errors"] = list(probe_errors)

    return v.finalize()
