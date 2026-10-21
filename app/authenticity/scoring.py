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


def _display_id(model_id: str) -> str:
    """Official short model id for user-facing text: strips the provider
    prefix only ('vsllm-o/kimi-k3' -> 'kimi-k3', 'glm-5.3-api' stays
    'glm-5.3-api'). Distinct from canonical_submodel(), which also folds
    '.'/'_' for equality tests -- display keeps the vendor's own slug."""
    s = (model_id or "").strip()
    return s.split("/")[-1] if "/" in s else s


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
    closest_candidates: list = field(default_factory=list)  # [{id, score}] ranked

    def finalize(self):
        self.confidence_level = confidence_level(self.confidence)
        self.timestamp = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        _build_conclusions(self)
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
                f"{self.identified_as or self.family or _display_id(self.claimed) or 'unknown'} "
                f"(confidence: {self.confidence_level.replace('_', ' ')}){mods}")

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "endpoint": self.endpoint,
            "claimed": self.claimed,
            "verdict": self.verdict,
            "identified_as": self.identified_as,
            "submodel_match": self.submodel_match,
            "closest_candidates": self.closest_candidates,
            "confidence": round(self.confidence, 3),
            "confidence_level": self.confidence_level,
            "family": self.family,
            "focus": self.focus,
            "modifications": [m.to_dict() for m in self.modifications],
            "evidence": self.evidence,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
            "headline": self.headline(),
            "conclusions": getattr(self, "conclusions", []),
        }


# ── Weights ────────────────────────────────────────────────────────────────
# ── Conclusions: primary (always) + ranked alternatives, each with rationale ──
_VERDICT_PHRASE = {
    "real": "serving the genuine claimed model",
    "downgrade": "real family but a lower/cheaper sub-version than claimed",
    "substitution": "a DIFFERENT model is being served under the claimed name",
    "family_match": "correct family, exact sub-version unconfirmed (no standard)",
    "unverifiable": "could not be confirmed - honest unknown, NOT a fake verdict",
    "error": "the test could not run to completion",
}


def _verdict_phrase(verdict: str) -> str:
    return _VERDICT_PHRASE.get(verdict, verdict.replace("_", " "))


def _build_conclusions(v) -> None:
    """Populate v.conclusions: PRIMARY = authenticity verdict (REAL/FAKE) first,
    then additional conclusions (fake-kind, behavior alternatives) at their own
    confidence zones. Every conclusion carries zone + rationale + evidence.

    The module's core objective is "is this model REAL or FAKE?", so rank-1 must
    always answer that before describing WHAT KIND of fake it is.
    """
    ev = v.evidence if isinstance(v.evidence, dict) else {}
    fc = ev.get("family_classifier") or {}
    behavior_scores = fc.get("behavior_scores") or {}
    impostor_hits = fc.get("impostor_hits") or ev.get("impostor_hits") or []
    self_hits = fc.get("self_hits") or []

    def zone(score: float) -> str:
        return confidence_level(max(0.0, min(1.0, score)))

    # ── Determine the REAL/FAKE authenticity verdict + its base confidence ──
    if v.verdict == "real":
        authenticity, auth_conf = "REAL", max(0.6, v.confidence)
        if v.submodel_match:
            auth_rationale = (
                f"Responses matched the frozen official standard for "
                f"'{v.submodel_match}' "
                f"(fused score {round(v.confidence, 2)}). The model is genuine."
            )
        else:
            auth_rationale = (
                f"Behavioral and style signals confirm family "
                f"'{v.identified_as or v.family}' "
                f"(fused score {round(v.confidence, 2)}), but no frozen standard "
                f"matched the exact sub-version. The model is a genuine "
                f"'{v.family}' family member; the precise sub-version is unconfirmed."
            )
        auth_ev = {"submodel_match": v.submodel_match, "submodel_score": v.submodel_score,
                   "alias_group": (v.evidence.get("match_dimensions", {}) or {}).get("_alias_group") or [],
                   "alias_detail": (v.evidence.get("match_dimensions", {}) or {}).get("_alias_detail") or {}}
    elif v.verdict in ("substitution", "downgrade"):
        authenticity = "FAKE"
        if v.verdict == "substitution":
            auth_conf = v.confidence
            if behavior_scores and str(v.identified_as) in behavior_scores:
                auth_conf = max(auth_conf, max(0.0, min(1.0, float(behavior_scores[str(v.identified_as)]))))
            nc_dim = (ev.get("match_dimensions") or {}).get("noncoding")
            nc_note = ""
            if isinstance(nc_dim, (int, float)):
                nc_note = (f" Non-coding judge similarity to the claimed standard is {round(nc_dim,2)} "
                           f"(<0.55 = different family) — the decisive discriminator.")
                auth_conf = max(auth_conf, min(0.95, 0.6 + (1.0 - nc_dim) * 0.35))
            auth_rationale = (
                f"The model claims to be '{_display_id(v.claimed) or v.family}' but its behavioral "
                f"fingerprint matches '{v.identified_as}' instead. Self-claims are zeroed; "
                f"only style/behavior signals are trusted. It is NOT the claimed model."
                + nc_note
            )
            auth_ev = {"behavior_scores": behavior_scores, "impostor_hits": impostor_hits,
                       "self_hits_ignored": self_hits}
            if isinstance(nc_dim, (int, float)):
                auth_ev["noncoding_similarity_to_claimed"] = round(nc_dim, 3)
        else:  # downgrade
            auth_conf = max(0.5, v.confidence)
            auth_rationale = (
                f"The family is real but the served model matched "
                f"'{v.submodel_match or v.identified_as}' - a cheaper/older sub-version than "
                f"the claimed '{_display_id(v.claimed)}'. Not the advertised model."
            )
            auth_ev = {"submodel_match": v.submodel_match, "claimed": v.claimed,
                       "alias_group": (v.evidence.get("match_dimensions", {}) or {}).get("_alias_group") or [],
                       "alias_detail": (v.evidence.get("match_dimensions", {}) or {}).get("_alias_detail") or {}}
    elif v.verdict == "error":
        authenticity, auth_conf = "UNVERIFIABLE", 0.0
        auth_rationale = "The test could not run to completion (endpoint/timeout/probe failure) - no real/fake determination possible."
        auth_ev = {"errors": ev.get("errors") or ev.get("probe_errors") or []}
    else:  # family_match / unverifiable -> honest cannot-confirm
        authenticity = "UNVERIFIABLE"
        if v.verdict == "family_match":
            auth_conf = v.confidence
            if behavior_scores and str(v.identified_as or v.family) in behavior_scores:
                auth_conf = max(auth_conf, max(0.0, min(1.0, float(behavior_scores[str(v.identified_as or v.family)]))))
            auth_rationale = (
                f"Style signals match family '{v.identified_as or v.family}', but no frozen "
                f"standard exists for the exact sub-version - cannot confirm REAL or FAKE."
            )
        else:
            auth_conf = v.confidence
            _already_deep = bool(ev.get("ran_full_battery"))
            _next = ("Even the full battery (identity + cutoff + capability + judge traits) could not "
                     "settle this - harvest a fresher standard from a trusted anchor, or pass a more "
                     "specific reference hint.") if _already_deep else (
                     "Only the standard battery ran here - re-run in auto mode and the full "
                     "battery escalates automatically on ambiguity, or pass a more specific "
                     "reference hint.")
            auth_rationale = (
                f"Signals were too ambiguous to confirm REAL or FAKE. "
                f"{ev.get('reason', 'no frozen standard or ambiguous fingerprint')} "
                f"{_next}"
            )
        auth_ev = {"reason": ev.get("reason", ""), "behavior_scores": behavior_scores,
                   "alias_group": (ev.get("match_dimensions", {}) or {}).get("_alias_group") or [],
                   "alias_detail": (ev.get("match_dimensions", {}) or {}).get("_alias_detail") or {},
                   "closest_candidates": [
                       {"id": c.get("id", ""), "score": c.get("score", 0.0)}
                       for c in (v.closest_candidates or [])]}

    # Presentation (user decision 2026-10-06): the top conclusion is ALWAYS
    # shown prominently. When confidence is below 'medium', label it 'Best
    # guess' and name the evidence gap so a weak read is never mistaken for a
    # firm verdict — but the best-supported answer still leads the report.
    _low_conf = auth_conf < 0.55
    _gap = ""
    if _low_conf:
        if v.verdict in ("unverifiable", "family_match"):
            _gap = "evidence gap: no frozen standard or ambiguous fingerprint"
        elif v.verdict == "error":
            _gap = "evidence gap: probes did not complete"
        else:
            _gap = "evidence gap: signals weak or conflicting"
    _identified_for_conclusion = (
        v.identified_as if v.verdict in ("real", "substitution", "downgrade") else ""
    )
    _cand_out = [{"id": c.get("id", ""), "score": c.get("score", 0.0),
                  "zone": zone(max(0.0, min(1.0, float(c.get("score", 0.0) or 0.0))))}
                 for c in (v.closest_candidates or [])]
    v.conclusions = [{
        "rank": 1, "primary": True, "verdict": authenticity.lower(),
        "label": authenticity,
        "identified_as": _identified_for_conclusion,
        "identified_kind": ("submodel" if v.submodel_match else "family") if _identified_for_conclusion else "",
        "closest_candidates": _cand_out,
        "zone": zone(auth_conf), "confidence": round(auth_conf, 3),
        "best_guess": _low_conf, "evidence_gap": _gap,
        "statement": f"The served model is {authenticity}" + (
            "" if authenticity == "REAL"
            else f" - not a genuine '{_display_id(v.claimed) or _display_id(v.model_id)}'"
            if authenticity == "FAKE"
            else " - real/fake could not be confirmed at model level."),
        "rationale": auth_rationale, "evidence": auth_ev,
    }]

    # ── Additional conclusion: the SPECIFIC finding (fake kind / family match) ──
    r = 2
    if v.verdict not in ("real", "error") and v.verdict != authenticity.lower():
        specific_rationale = {
            "substitution": f"Specifically a SUBSTITUTION - a different model family ('{v.identified_as}') is served under the claimed name.",
            "downgrade": f"Specifically a DOWNGRADE - real family, but a cheaper/older sub-version ('{v.identified_as}') than claimed.",
            "family_match": f"Specifically a FAMILY MATCH - correct family, sub-version unconfirmed.",
        }.get(v.verdict, "")
        if specific_rationale:
            v.conclusions.append({
                "rank": r, "primary": False, "verdict": v.verdict,
                "label": v.verdict.upper().replace("_", " "),
                "identified_as": v.identified_as, "zone": zone(v.confidence),
                "confidence": round(v.confidence, 3),
                "statement": _verdict_phrase(v.verdict),
                "rationale": specific_rationale,
                "evidence": auth_ev,
            })
            r += 1

    # ── Additional conclusions: behavior alternatives from behavior_scores ──
    try:
        ranked = sorted(behavior_scores.items(), key=lambda kv: kv[1], reverse=True)
    except Exception:
        ranked = []
    for fam, sc in ranked:
        if r > 4:
            break
        fam_s = str(fam)
        if fam_s == str(v.identified_as) or fam_s == str(v.family) or fam_s == str(v.claimed).lower():
            continue
        if fam_s in ("unknown", "claimed", "impostor", "injection"):
            continue
        alt_conf = max(0.0, min(1.0, float(sc)))
        v.conclusions.append({
            "rank": r, "primary": False, "verdict": "could_be",
            "label": "could be " + fam_s, "identified_as": fam_s,
            "zone": zone(alt_conf), "confidence": round(alt_conf, 3),
            "statement": "behavioral style also scores toward '" + fam_s + "'",
            "rationale": (f"Style fingerprint gave '{fam_s}' a score of {round(alt_conf, 2)}. "
                          f"Lower than the primary conclusion, so kept as an alternative."),
            "evidence": {"behavior_score": round(alt_conf, 3)},
        })
        r += 1

    # Sync verdict-level confidence/level to the primary (strongest signal).
    v.confidence = auth_conf
    v.confidence_level = zone(auth_conf)


FAMILY_WEIGHT = 0.5
SUBMODEL_WEIGHT = 0.5

REAL_THRESHOLD = 0.80
SUBSTITUTION_THRESHOLD = 0.30
# FIX G (2026-10-13): a tie between sibling standards resolved by an ACCEPTED
# judge pick (order-consistent across both candidate orders, not contradicted
# by the decisive v2 lanes -- see gate.py) that EQUALS the claimed sub-model is
# confirmation, not ambiguity. The gate only accepts such a pick when the
# deterministic score is already >= 0.70, so the fused bar is set there too.
# Live run 3: genuine glm-5.3, judge picked glm-5-3 (0.88) and the verdict
# still read UNVERIFIABLE at fused 0.756 < 0.80.
JUDGE_CONFIRMED_THRESHOLD = 0.70

# Sub-tier suffixes that name a DIFFERENT (cheaper) model, never an alias.
_SUBTIER_TOKENS = {"flash", "mini", "lite", "nano", "air"}


def canonical_submodel(name: str) -> str:
    """'mintrouter/claude-opus-5-5' -> 'claude-opus-5-5'; 'ZHIPU/GLM-5.3' ->
    'glm-5-3'; 'glm_glm-4.6' -> 'glm-4-6'. Provider/family prefixes dropped,
    '.' and '_' unified to '-'."""
    n = (name or "").strip().lower()
    n = n.rsplit("/", 1)[-1]
    n = n.rsplit("_", 1)[-1] if "_" in n else n
    return n.replace(".", "-").strip("-")


def same_submodel(a: str, b: str) -> bool:
    """True when a and b name the same sub-model. One may extend the other only
    by a non-version, non-sub-tier suffix ('kimi-k3' ~ 'kimi-k3-api'). A numeric
    suffix is a different version ('claude-opus-5' vs 'claude-opus-5-5' -- the
    old substring test called these equal) and a sub-tier suffix is a different
    model ('glm-5-3' vs 'glm-5-3-flash')."""
    ca, cb = canonical_submodel(a), canonical_submodel(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    short, long_ = (ca, cb) if len(ca) < len(cb) else (cb, ca)
    if not long_.startswith(short + "-"):
        return False
    first = long_[len(short) + 1:].split("-", 1)[0]
    return not (first.isdigit() or first in _SUBTIER_TOKENS)


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
    # Bind defaults BEFORE the match block: when match_result is None (no
    # baselines on disk, or Tier 2 skipped in normal mode with max_tier=1),
    # the REAL_THRESHOLD branch below used to hit `matched` unbound
    # (UnboundLocalError wrapped as an error verdict by the gate).
    matched = ""
    sub_score = 0.0
    if match_result is not None:
        sub_score = getattr(match_result, "match_score", 0.0)
        matched = getattr(match_result, "matched_model", "")
        v.submodel_match = matched
        v.submodel_score = sub_score
        _dims = getattr(match_result, "dimensions", None)
        if isinstance(_dims, dict) and _dims:
            v.evidence["match_dimensions"] = _dims
            if _dims.get("judge_tiebreak"):
                v.evidence["judge_tiebreak"] = _dims["judge_tiebreak"]
        # Closest candidates for the report (ranked; the matcher abstains on
        # matched_model when discrimination is weak, but the ranked scores are
        # still the honest best-guess ordering).
        _cands = []
        # FIX G: after an accepted judge pick, matched_model (with its own
        # score) leads; best_model is the displaced deterministic leader.
        _bm = matched or getattr(match_result, "best_model", "")
        _ru = getattr(match_result, "runner_up", "")
        _ru_sc = getattr(match_result, "runner_up_score", 0.0)
        if _bm:
            _cands.append({"id": _bm, "score": round(sub_score, 3)})
        if _ru and _ru != _bm:
            _cands.append({"id": _ru, "score": round(_ru_sc, 3)})
        v.closest_candidates = _cands
        # Full per-candidate breakdown (every standard in the pool, ranked):
        # explains WHY the order is what it is, lane by lane.
        _ranked = getattr(match_result, "ranked", None)
        if isinstance(_ranked, list) and _ranked:
            v.evidence["ranked_candidates"] = _ranked

    if scope_info.get("verdict_scope") == "full" and match_result and not getattr(match_result, "abstain", True):
        total = FAMILY_WEIGHT * fam_score + SUBMODEL_WEIGHT * sub_score
    else:
        total = fam_score  # family-only path

    v.confidence = total

    # Family confidence promotion: a strong sub-model match (>= 0.70) against
    # a standard whose family matches the detected/claimed family IMPLIES family
    # membership. Promote confidence so the report doesn't show the conflict
    # "model 70-75% but family LOW".
    if matched and sub_score >= 0.70 and v.confidence < 0.65:
        v.confidence = max(v.confidence, 0.65)

    # FIX G2 (2026-10-13): the claimed sub-model. Without a Reference hint
    # scope focus is empty, so the downgrade check never ran -- live run 3
    # reported 'REAL - claude-fable-5-1' for a model sold as claude-opus-5-5.
    # The model id the seller exposes IS the claim; derive it when no hint.
    _focus = scope_info.get("focus") or ""
    if not _focus and scope_info.get("verdict_scope") == "full":
        _focus = canonical_submodel(claimed or model_id)
    if _focus:
        v.evidence["claimed_submodel"] = _focus
    _jt = (getattr(match_result, "dimensions", None) or {}).get("judge_tiebreak") \
        if match_result is not None else None
    _judge_confirms_claim = bool(
        matched and _focus and isinstance(_jt, dict) and _jt.get("accepted")
        and _jt.get("picked") == matched and same_submodel(_focus, matched))

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
    elif matched and (total >= REAL_THRESHOLD
                      or (_judge_confirms_claim and total >= JUDGE_CONFIRMED_THRESHOLD)):
        # REAL requires a CONFIRMED sub-model standard match - that is the
        # actual "is this the model it claims to be" answer. Downgrade check:
        # matched a different sub-model than the claimed/focus one.
        v.identified_as = matched
        if _focus and not same_submodel(_focus, matched):
            v.verdict = "downgrade"
        else:
            v.verdict = "real"
            if _judge_confirms_claim and total < REAL_THRESHOLD:
                v.evidence["resolved_by"] = "judge_tiebreak"
    elif total >= REAL_THRESHOLD:
        # Family confirmed at high confidence but NO sub-model standard
        # matched (matcher abstained or no standards exist). That is NOT
        # "real" - the real/fake question is unanswered at model level. Report
        # FAMILY MATCH with ranked closest candidates so the user sees the
        # best-supported model guesses at their own confidence, not a shrug
        # dressed as REAL.
        v.identified_as = claimed_family or behavior_family or v.family or "unknown"
        v.verdict = "family_match"
        v.evidence["submodel_gap"] = (
            "family-level signals only; no frozen sub-model standard matched")
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
        v.evidence["reason"] = "ambiguous fingerprint - no decisive winner among the candidates"

    # Prompt-injection is a MODIFICATION, not a fake verdict
    if getattr(family_verdict, "injection_detected", False):
        v.modifications.append(Modification(
            type="system_prompt_injection",
            detail="surface self-claim disagrees with behavioral fingerprint",
        ))

    if probe_errors:
        v.evidence["probe_errors"] = list(probe_errors)

    return v.finalize()
