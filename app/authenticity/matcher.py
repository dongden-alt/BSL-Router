"""Deterministic sub-model matching against baselines (BazaarLink Layer 2)."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from .judge_rank import coverage_adjust, FEATURE_WEIGHTS
from .knowledge_needles import fact_text, is_disclaimer_fact
from .standard_v2 import compute_v2_dims, pairwise_separability, v2_of

# Soft vetoes for the v2 dimensions (same shape as the logprob/boundary vetoes:
# a COMPARED dimension far below these on the would-be winner forces abstain).
# Conservative until calibrated against live self-match distributions.
V2_VETO = {"knowledge_bank": 0.45, "capability_curve": 0.40, "tokenizer": 0.50,
           "discriminator": 0.45, "token_jsd": 0.45}

# Discriminator veto near-miss instrumentation (2026-10-10): every time the
# would-be winner's compared discriminator sim lands under the V2_VETO floor,
# log ONE JSON line for offline threshold calibration. Capped at ~1MB (last
# half kept on overflow), repo-root anchored, and NEVER raises.
_DISC_NEARMISS_LOG = (Path(__file__).resolve().parents[2]
                      / ".brain" / "scratch" / "disc_veto_nearmiss.jsonl")


def _log_disc_veto_nearmiss(features, winner: str, sim: float) -> None:
    try:
        candidate = (getattr(features, "model_id", None)
                     or getattr(features, "model", None) or "?")
        logger.info("[disc-nearmiss] candidate=%s sim=%.3f winner=%s",
                    candidate, sim, winner)
        p = _DISC_NEARMISS_LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 1_000_000:
            p.write_bytes(p.read_bytes()[-500_000:])
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "candidate": candidate,
                "disc_sim": sim,
                "would_be_winner": winner,
            }) + "\n")
    except Exception:
        pass


@dataclass
class MatchResult:
    matched_model: str        # best-matching model from baseline
    match_score: float        # 0.0-1.0
    runner_up: str            # second-best model
    runner_up_score: float
    abstain: bool             # True if insufficient discrimination
    dimensions: dict          # per-dimension match details
    best_model: str = ""      # ranked top model, kept even when abstaining
    effective_weights: dict = field(default_factory=dict)  # coverage-adjusted
    alias_group: list = field(default_factory=list)  # tied siblings v2 cannot separate
    # Per-candidate breakdown, ranked best-first: [{"id", "score", "dims"}].
    # Previously only the WINNER's dims were kept (plus the runner-up's total),
    # so "why is model X second?" could not be answered from the verdict and had
    # to be reconstructed offline. Purely additive - nothing reads it.
    ranked: list = field(default_factory=list)


_SELF_REPORTED_CUTOFF_RE = r"(?i)self-reported\s+(\d{4})-(\d{1,2})"


def _self_reported_cutoff(baseline: dict):
    """Extract the model's honest self-reported cutoff from a standard's
    meta.cutoff_override, e.g. "user-stated 2026-02-16 (model self-reported
    2024-06)" -> (2024, 6). Fail-open: absent or unparseable -> None, so
    behaviour is identical to standards without the field."""
    import re
    raw = (baseline.get("meta") or {}).get("cutoff_override") or ""
    m = re.search(_SELF_REPORTED_CUTOFF_RE, raw)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except (TypeError, ValueError):
        return None


def _cutoff_score(feat_year: int, feat_month: int,
                  base_year: int, base_month: int) -> float:
    """Cutoff-dimension rules (unchanged from the pre-FIX-L inline block):
    same year -> 0.8 if either month unknown, 1.0 on exact month, 0.8 within
    one month, else 0.3; year off by exactly one -> 0.3; wider -> 0.0;
    either side unknown -> neutral 0.5."""
    if feat_year > 0 and base_year > 0:
        if feat_year == base_year:
            if not feat_month or not base_month:
                # FIX I (2026-10-13): month 0 = UNKNOWN month, not a
                # mismatch. glm-5-3's standard is 2024-0, so the genuine
                # model scored 0.3 (month-mismatch) on a year agreement.
                return 0.8
            if feat_month == base_month:
                return 1.0
            if abs(feat_month - base_month) <= 1:
                return 0.8
            return 0.3
        if abs(feat_year - base_year) == 1:
            return 0.3
        return 0.0
    return 0.5  # unknown — neutral


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
            best_model="",
        )

    scores: dict[str, float] = {}
    all_dimensions: dict[str, dict] = {}

    # Standard v2: judge-free dims need ALL candidates at once (the knowledge
    # bank is scored on the items that actually split these candidates).
    v2_sus = getattr(features, "v2_profile", {}) or {}
    try:
        v2_dims = compute_v2_dims(v2_sus, baselines) if v2_sus else {}
    except Exception:
        v2_dims = {}  # v2 is additive evidence; never let it break matching

    for model_id, baseline in baselines.items():
        score = 0.0
        dims: dict[str, float] = {}

        # Cutoff match (weight: 0.5)
        # Standards store cutoff at knowledge.cutoff_year/month (TraitMark
        # structure); legacy flat baselines may have top-level keys. Check both.
        _know = baseline.get("knowledge") or {}
        baseline_year = baseline.get("cutoff_year", 0) or _know.get("cutoff_year", 0)
        baseline_month = baseline.get("cutoff_month", 0) or _know.get("cutoff_month", 0)
        dims["cutoff"] = _cutoff_score(features.cutoff_year, features.cutoff_month,
                                       baseline_year, baseline_month)
        # FIX L (2026-10-13): contested cutoff - see
        # .brain/cc_tasks/authenticity_cutoff_override.md. When the standard
        # records that its stored cutoff is user-asserted and differs from the
        # model's honest self-report (meta.cutoff_override), score against BOTH
        # dates and keep the max: a genuine model matching EITHER must not be
        # penalized, while a date matching NEITHER still scores low.
        if dims["cutoff"] < 1.0:
            _sr_cutoff = _self_reported_cutoff(baseline)
            if _sr_cutoff is not None:
                dims["cutoff"] = max(dims["cutoff"], _cutoff_score(
                    features.cutoff_year, features.cutoff_month,
                    _sr_cutoff[0], _sr_cutoff[1]))

        score += dims["cutoff"] * 0.5

        # Capability match (weight: 0.5)
        # TraitMark stores at knowledge.capability_boundary.correct
        _cap_boundary = _know.get("capability_boundary") or {}
        baseline_correct = (baseline.get("capability_correct", 0)
                           or _cap_boundary.get("correct", 0))
        if baseline_correct > 0:
            dims["capability"] = features.capability_correct / max(baseline_correct, 1)
        else:
            dims["capability"] = 0.5

        score += dims["capability"] * 0.5

        # NON-CODING (deep layer): family discriminator via judge profile.
        # TraitMark stores scores at noncoding.{axis}.score directly (NOT
        # nested under .judge). Legacy baselines may use .judge sub-key.
        nc_sus = (getattr(features, "noncoding", {}) or {}).get("judge") or {}
        _nc_base_raw = baseline.get("noncoding") or {}
        nc_base = _nc_base_raw.get("judge") or {}
        # If .judge is empty, read axes directly from TraitMark structure
        if not nc_base and _nc_base_raw:
            nc_base = _nc_base_raw  # axes are top-level keys in TraitMark
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
        #
        # 2026-10-08 FIXES: (a) standards store needles as {fact, needle}
        # dicts while the suspect side emits fact STRINGS -- the old raw
        # `dict in set` membership raised TypeError (swallowed fail-open in the
        # gate, silently killing the whole post-needle cascade). Everything is
        # now compared through fact_text(). (b) Only facts the suspect was
        # ACTUALLY ASKED count: the probe samples <=12 of the family's pooled
        # needles, so scoring unasked facts as 'missed' capped every candidate
        # (including the genuine one) near 0. (c) Disclaimer 'facts' are dropped.
        kn_sus = getattr(features, "knowledge_needles", {}) or {}
        knowledge = baseline.get("knowledge") or {}
        base_known = [fact_text(e) for e in (knowledge.get("known_facts") or [])]
        base_post = [fact_text(e) for e in (knowledge.get("post_cutoff_unknown") or [])]
        base_known = [f for f in base_known if f and not is_disclaimer_fact(f)]
        base_post = [f for f in base_post if f and not is_disclaimer_fact(f)]
        kn_answers = kn_sus.get("answers") or []
        dims["knowledge"] = None  # not compared unless proven below
        if (base_known or base_post) and kn_answers:
            # FIX D: an item with NO parsed answer line (reply truncated /
            # failed mid-stream) was never answered -> not evidence. It used
            # to count as a known-fact miss. An explicit 'unknown' is a real
            # answer and still counts.
            asked = {fact_text(a.get("fact")) for a in kn_answers
                     if isinstance(a, dict) and a.get("fact")
                     and (a.get("answer") or "").strip()}
            if asked:
                k_scope = [f for f in base_known if f in asked]
                p_scope = [f for f in base_post if f in asked]
            else:  # legacy shape: no per-item answers -> whole-list scope
                k_scope, p_scope = base_known, base_post
            n_scope = len(k_scope) + len(p_scope)
            if n_scope >= 2:  # 1 item is a coin flip, not evidence
                known_hits = {fact_text(h) for h in kn_sus.get("known_hits", [])}
                post_claimed = {fact_text(h) for h in kn_sus.get("post_cutoff_claimed", [])}
                right = (sum(1 for f in k_scope if f in known_hits)
                         + sum(1 for f in p_scope if f not in post_claimed))
                dims["knowledge"] = round(right / n_scope, 3)


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
            # FIX D: a failed stop probe yields default False fields, not
            # behavior. Legacy signatures (no stop_mode) count as observed.
            skip_stop = (b_sus.get("stop_mode") == "error"
                         or b_base.get("stop_mode") == "error")
            fields = [f for f in _b_fields
                      if not (skip_json and f == "json_strict")
                      and not (skip_stop and f in ("stop_strict", "stop_echo"))]
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
        def _st_valid(p: dict) -> int:
            # FIX D: legacy profiles lack valid_samples -> count non-empty hashes.
            v = p.get("valid_samples")
            if isinstance(v, int):
                return v
            return sum(1 for h in (p.get("hashes") or []) if h)

        if (st_sus and st_base and st_sus.get("mode_hash") and st_base.get("mode_hash")
                and _st_valid(st_sus) >= 2 and _st_valid(st_base) >= 2):
            _sd, _bd = st_sus.get("distinct_hashes"), st_base.get("distinct_hashes")
            if (isinstance(_sd, int) and isinstance(_bd, int) and (_sd > 1 or _bd > 1)):
                # FIX K (2026-10-13): EITHER side non-deterministic at temp=0
                # -> not compared. Both non-deterministic: the mode hash is one
                # arbitrary sample and can never match. Class mismatch (one
                # deterministic, one not): serving-side batching, not weights --
                # live run 3 hit genuine glm-5.3 on AI-BOX, the SAME provider
                # the gold standard was harvested from (distinct=1), with a
                # non-deterministic reply -> stability 0.0 + veto risk.
                dims["stability"] = None
            elif st_sus.get("mode_hash") == st_base.get("mode_hash"):
                dims["stability"] = 1.0
            else:
                # both deterministic, different content
                dims["stability"] = 0.4
        else:
            dims["stability"] = None  # either side missing/empty

        # REFUSAL/ALIGNMENT (family-level corroborator): the zh-vs-en refusal
        # delta per axis fingerprints the alignment regime. Compared ONLY when
        # BOTH sides carry non-empty delta data; missing axis = None (coverage-
        # aware: neutral, not a penalty). NO VETO POWER — corroborator only.
        r_sus = getattr(features, "refusal_signature", {}) or {}
        r_base = (baseline.get("refusal") or {})
        r_base_delta = r_base.get("delta_signature") or {}
        r_sus_delta = r_sus.get("delta") or {}
        if r_sus_delta and r_base_delta:
            shared_axes = [a for a in r_sus_delta if a in r_base_delta]
            if shared_axes:
                matched = sum(
                    1 for a in shared_axes
                    if abs(r_sus_delta[a] - r_base_delta[a]) <= 0.5
                )
                dims["refusal"] = round(matched / len(shared_axes), 3)
            else:
                dims["refusal"] = None
        else:
            dims["refusal"] = None  # either side missing/empty

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

        for _d2 in ("tokenizer", "capability_curve", "knowledge_bank",
                    "discriminator", "spatial_curve", "token_jsd"):
            dims[_d2] = (v2_dims.get(model_id) or {}).get(_d2)

        scores[model_id] = score  # legacy 2-dim base (cutoff+capability), audit only
        all_dimensions[model_id] = dims

    # RANKING (2026-10-08 fix). The old code ranked candidates by the 2-dim
    # `score` (cutoff + capability) ALONE; noncoding / knowledge / logprob /
    # boundary / stability / refusal only acted as post-hoc vetoes on whoever
    # had already won. When cutoff is honest-unknown and every sibling passes
    # the same capability probe (the normal case) all siblings tied and the
    # winner was simply the FIRST ALPHABETICAL key (claude-fable-5-1) -- the
    # "opus-5-5 detected as fable-5/fable-5-1" bug. Rank on the full weighted
    # evidence instead. Dimensions nobody can compare are dropped for ALL
    # candidates; a dimension only SOME candidates carry is neutral (0.5) for
    # the others, so thin coverage is neither a bonus nor a penalty.
    compared_any = [d for d in FEATURE_WEIGHTS
                    if any(all_dimensions[m].get(d) is not None for m in all_dimensions)]
    _w_tot = sum(FEATURE_WEIGHTS[d] for d in compared_any) or 1.0

    def _rank_score(dims_: dict) -> float:
        return sum(FEATURE_WEIGHTS[d] * (dims_.get(d) if dims_.get(d) is not None else 0.5)
                   for d in compared_any) / _w_tot

    rank_scores = {m: _rank_score(all_dimensions[m]) for m in all_dimensions}
    ranked = sorted(rank_scores.items(), key=lambda x: x[1], reverse=True)
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
        elif (nc >= 0.8 and abstain and best_score >= 0.55
              and (best_score - runner_up_score) >= 0.05):
            # Rescue ONLY a low-score abstention with a clear gap. A TIE
            # (gap < 0.05) is not rescuable: sibling versions share a family
            # writing style, so non-coding agrees with ALL of them and would
            # un-abstain onto an arbitrary sibling.
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

    # Standard v2 soft vetoes: a compared v2 dimension far below its floor on
    # the would-be winner means the weights are not the claimed standard's.
    for _d2, _floor in V2_VETO.items():
        _v = best_dims.get(_d2)
        if _v is not None:
            if _v < _floor and not abstain:
                abstain = True
            if _d2 == "discriminator" and isinstance(_v, float) and _v < _floor:
                _log_disc_veto_nearmiss(features, best_model, _v)
            best_dims = dict(best_dims)
            best_dims[_d2] = _v

    # ALIAS GROUP: top candidates inside the tie band whose standards the v2
    # evidence cannot tell apart (or has no data to tell apart). Honest output
    # is 'family confirmed, sub-version ambiguous between {A,B}', not a pick.
    alias_group: list = []
    _tied = [m for m, s in ranked[1:] if (best_score - s) < 0.05]
    if _tied:
        _detail = {}
        for m in _tied:
            try:
                _sep = pairwise_separability(baselines.get(best_model) or {},
                                             baselines.get(m) or {})
            except Exception:
                _sep = {"separable": None, "distance": None}
            _detail[m] = {"distance": _sep.get("distance"),
                          "separable": _sep.get("separable")}
            if _sep.get("separable") is not True:
                alias_group.append(m)
        if alias_group:
            alias_group = [best_model] + alias_group
            best_dims = dict(best_dims)
            best_dims["_alias_group"] = alias_group
            best_dims["_alias_detail"] = _detail

    # v2 diagnostic: when the suspect carried v2 lanes but every compared v2
    # dim is None for the winner, record WHICH side lacked data so the next
    # live run is self-explaining (suspect lane empty vs baseline lane absent).
    # Purely additive; coverage_adjust only reads FEATURE_WEIGHTS keys, so the
    # extra key never affects scoring.
    if v2_sus:
        _v_lanes = [k for k in ("tokenizer", "capability_curve",
                                "knowledge_bank", "discriminator") if v2_sus.get(k)]
        if _v_lanes and all(best_dims.get(_d) is None for _d in _v_lanes):
            _base_v2 = v2_of(baselines.get(best_model) or {})
            best_dims = dict(best_dims)
            best_dims["_v2_diag"] = {
                "suspect_lanes": _v_lanes,
                "baseline_lanes": [_d for _d in _v_lanes if _base_v2.get(_d)],
            }
    else:
        # Suspect carried NO v2 profile (live lane unreachable / every lane
        # budget expired) while the winner's baseline HAS v2 data: record the
        # attribution so an all-None v2 verdict points at the suspect side.
        _base_v2 = v2_of(baselines.get(best_model) or {})
        _b_lanes = [k for k in ("tokenizer", "capability_curve",
                                "knowledge_bank", "discriminator") if _base_v2.get(k)]
        if _b_lanes:
            best_dims = dict(best_dims)
            best_dims["_v2_diag"] = {"suspect_lanes": [], "baseline_lanes": _b_lanes}

    # Coverage-aware rescale: re-normalize the winner's dimension weights over
    # the dims actually compared, so a missing axis is neutral rather than an
    # implicit penalty. The raw base score stays in dimensions for audit.
    eff = coverage_adjust(best_dims)
    if eff:
        best_score = sum((best_dims.get(d) or 0.0) * w for d, w in eff.items())
        best_dims["_effective_weights"] = {k: round(v, 3) for k, v in eff.items()}
        # Score the runner-up on the SAME dimensions and weights as the winner
        # (missing -> neutral 0.5). Previously match_score was a full-dimension
        # figure while runner_up_score stayed the 2-dim base, so the gap the
        # gate/judge tie-break reads compared two different quantities.
        if runner_up in all_dimensions:
            _ru = all_dimensions[runner_up]
            runner_up_score = sum(
                (_ru.get(d) if _ru.get(d) is not None else 0.5) * w
                for d, w in eff.items())

    # Per-candidate breakdown, ranked best-first. The winner's score is the
    # coverage-adjusted one; the others are scored on the SAME dimensions and
    # weights so the ranking is comparable (missing -> neutral 0.5).
    _ranked_out = []
    for _m, _s in ranked:
        _d = all_dimensions.get(_m, {})
        if _m == best_model:
            _sc = best_score
        elif eff:
            _sc = sum(
                (_d.get(k) if _d.get(k) is not None else 0.5) * w
                for k, w in eff.items())
        else:
            _sc = _s
        _ranked_out.append({
            "id": _m,
            "score": round(float(_sc), 3),
            "dims": {k: v for k, v in _d.items() if not k.startswith("_")},
        })

    return MatchResult(
        matched_model=best_model if not abstain else "",
        match_score=round(best_score, 3),
        runner_up=runner_up,
        runner_up_score=round(runner_up_score, 3),
        abstain=abstain,
        dimensions=best_dims,
        best_model=best_model,
        effective_weights=eff,
        alias_group=alias_group,
        ranked=_ranked_out,
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
