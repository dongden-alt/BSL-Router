"""Judge tie-break: rank candidate standards against the suspect's profile.

Used ONLY when the deterministic matcher reports a true tie (two or more
frozen standards within a hair of each other) — the <0.05 best/runner-up gap.
The judge model (a DIFFERENT family, routed through this router) compares the
suspect's behavioral/lexical/non-coding profile against each tied candidate's
frozen standard and returns the closest. Fail-open: any error -> no override.
"""
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

# Feature -> weight (must sum to 1.0). Identity self-claims are EXCLUDED (a
# spoofed model self-reports the claim). Dimensions not present on a given
# candidate are excluded and the remaining weights re-normalize (coverage-
# aware scoring — a missing axis is neutral, not a penalty).
FEATURE_WEIGHTS = {
    # Rebalanced 2026-10-10 (zero-call canonical sweep, 18 standards, 7 families):
    # the discriminator axis was highly separable but silenced at 0.03 (eff 0.037),
    # so sibling pairs tied inside the <0.05 abstain gap (opus-5/5-5 -> abstain).
    # Raised to 0.15; funded by demoting axes that provably do NOT separate
    # siblings (every sibling passes the same capability probe and shares a family
    # writing style, so noncoding/capability/cutoff tie). Sweep: 13/18 -> 17/18
    # clean self-picks, zero family regressions (gpt 2/4 -> 4/4 as a bonus).
    "cutoff": 0.16,
    "capability": 0.06,        # v1 3-question probe: every model passes -> demoted
    "noncoding": 0.07,
    "knowledge": 0.09,
    "boundary": 0.05,
    "stability": 0.02,
    "logprob": 0.03,
    "refusal": 0.05,   # family corroborator — low weight, never decisive
    # Standard v2 (judge-free, fresh-instance / truth-free; see standard_v2.py)
    "knowledge_bank": 0.15,
    "capability_curve": 0.12,
    "tokenizer": 0.05,
    "discriminator": 0.15,     # shared-probe answer agreement; sub-version discriminator
}

_RANK_PROMPT = """You are a forensic model-identification judge. A suspect model produced the behavioral profile below. Compare it against each candidate frozen standard and decide which candidate the suspect most likely is.

SUSPECT PROFILE (numeric features only; self-claims are stripped as untrusted):
{suspect}

CANDIDATE STANDARDS:
{candidates}

Return ONLY a JSON object: {{"best": <candidate index int>, "confidence": <0.0-1.0 float>, "why": "<one sentence>"}}. Confidence reflects how decisive the evidence is, NOT how good a model the suspect is. If the profile is too thin to discriminate, return confidence < 0.4."""


def _suspect_profile(features) -> dict:
    """Flatten the suspect's comparable features (identity self-claims removed)."""
    nc = (getattr(features, "noncoding", {}) or {}).get("judge") or {}
    out = {
        "cutoff_year": getattr(features, "cutoff_year", 0),
        "cutoff_month": getattr(features, "cutoff_month", 0),
        "capability_correct": getattr(features, "capability_correct", 0),
        "avg_sentence_length": getattr(features, "avg_sentence_length", 0.0),
        "cjk_ratio": getattr(features, "cjk_ratio", 0.0),
        "bullet_style": getattr(features, "bullet_style", ""),
        "header_depth": getattr(features, "header_depth", 0),
        "noncoding_scores": {a: (v or {}).get("score") for a, v in nc.items() if isinstance(v, dict)},
    }
    kn = getattr(features, "knowledge_needles", {}) or {}
    if kn:
        out["knowledge"] = {k: kn.get(k) for k in ("known_hits", "known_misses", "post_cutoff_claimed") if k in kn}
    # Refusal/alignment family corroborator: delta dict + per-axis verdicts.
    # Templates are deliberately EXCLUDED (free-text, prompt-injectable).
    rf = getattr(features, "refusal_signature", {}) or {}
    if rf and rf.get("axes"):
        out["refusal"] = {
            "delta": dict(rf.get("delta") or {}),
            "verdicts": {
                axis: {lang: ((langs or {}).get(lang) or {}).get("verdict", "")
                       for lang in ("en", "zh")}
                for axis, langs in (rf.get("axes") or {}).items()
            },
        }
    return out


def _candidate_summary(key: str, std: dict) -> dict:
    """Compact per-candidate standard summary for the judge prompt.

    Standards are TraitMark dicts: cutoff/capability live under ``knowledge``.
    Legacy flat baselines carry them top-level -- read both (nested wins).
    """
    style = std.get("style") or {}
    lex = std.get("lexical") or {}
    nc = std.get("noncoding") or {}
    know = std.get("knowledge") or {}
    cap = know.get("capability_boundary") or {}
    refusal = std.get("refusal") or {}
    ts = (std.get("structural") or {}).get("tokenizer_signature") or {}
    return {
        "id": key,
        "cutoff_year": know.get("cutoff_year") or std.get("cutoff_year"),
        "cutoff_month": know.get("cutoff_month") or std.get("cutoff_month"),
        "capability_correct": cap.get("correct") or std.get("capability_correct"),
        "style": {k: style.get(k) for k in ("avg_sentence_length", "cjk_ratio", "bullet_style", "header_depth") if style.get(k) is not None},
        "lexical": {k: lex.get(k) for k in ("slop_score", "vocabulary_complexity") if lex.get(k) is not None},
        "noncoding": {a: (nc.get(a) or {}).get("score") if isinstance(nc.get(a), dict) else nc.get(a) for a in ("creative", "eq", "humor", "design") if nc.get(a) is not None},
        "known_facts_count": len(know.get("known_facts") or []),
        "post_cutoff_count": len(know.get("post_cutoff_unknown") or []),
        "refusal_delta": dict(refusal.get("delta_signature") or {}),
        "boundary": {k: (ts.get("boundary_signature") or {}).get(k)
                     for k in ("stop_strict", "stop_echo", "json_strict")
                     if (ts.get("boundary_signature") or {}).get(k) is not None},
    }


async def rank_candidates(
    judge_endpoint: str,
    judge_key: str,
    judge_model: str,
    features,
    candidates: dict,
    timeout_s: int = 180,
):
    """Rank tied candidate standards; return {"id","confidence","why"} or None.

    candidates: {standard_key: standard_dict} for the tied top candidates.
    NEVER raises; returns None on any failure / non-decisive / parse error.
    """
    if not (judge_endpoint and judge_key and judge_model) or not candidates:
        return None
    try:
        suspect = json.dumps(_suspect_profile(features), ensure_ascii=False)
        cand_text = "\n\n".join(
            f"[{i}] " + json.dumps(_candidate_summary(k, s), ensure_ascii=False)
            for i, (k, s) in enumerate(candidates.items())
        )
        keys = list(candidates.keys())
        prompt = _RANK_PROMPT.format(suspect=suspect, candidates=cand_text)
        base = judge_endpoint.rstrip("/")
        url = (f"{base}/v1/chat/completions" if not base.endswith("/v1")
               else f"{base}/chat/completions")
        payload = {"model": judge_model,
                   "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": 300, "temperature": 0}
        headers = {"Authorization": f"Bearer {judge_key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning("judge rank_candidates: %s returned HTTP %s | body=%r",
                               judge_model, resp.status_code, (resp.text or "")[:300])
                return None
            data = resp.json()
            content = ((data.get("choices") or [{}])[0]
                       .get("message", {}) or {}).get("content", "") or ""
        # Extract the JSON object from the judge's reply.
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return None
        obj = json.loads(content[start:end + 1])
        idx = obj.get("best")
        conf = obj.get("confidence")
        if not isinstance(idx, int) or not (0 <= idx < len(keys)):
            return None
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            return None
        return {"id": keys[idx], "confidence": round(conf, 3),
                "why": str(obj.get("why", ""))[:200]}
    except Exception as exc:  # fail-open: judge never blocks the pipeline
        logger.warning("judge rank_candidates failed (fail-open): %s", exc)
        return None


def coverage_adjust(dimensions: dict) -> dict:
    """Re-normalize FEATURE_WEIGHTS over the dimensions actually compared.

    A dimension with a None value (not compared) is excluded and the rest of
    the weights scale up to sum to 1.0, so a missing axis neither inflates nor
    deflates the score. Returns {dim: effective_weight}.
    """
    present = [d for d in FEATURE_WEIGHTS if dimensions.get(d) is not None]
    if not present:
        return {}
    total = sum(FEATURE_WEIGHTS[d] for d in present)
    return {d: FEATURE_WEIGHTS[d] / total for d in present}


# Judge-free, truth-free lanes that actually split sibling versions (see the
# 2026-10-10 canonical sweep). Style axes (sentence length, header depth) are
# deliberately NOT here: they are within-family noise.
DECISIVE_LANES = ("knowledge_bank", "capability_curve", "tokenizer", "discriminator")
CONTRADICTION_MARGIN = 0.02


def decisive_lane_margin(ranked: list, a_id: str, b_id: str):
    """Weighted mean advantage of b over a on the decisive lanes both carry.

    ranked: MatchResult.ranked ([{"id","score","dims"}]). Returns None when the
    two candidates share no decisive lane (nothing to compare)."""
    dims = {c.get("id"): (c.get("dims") or {}) for c in (ranked or [])
            if isinstance(c, dict)}
    da, db = dims.get(a_id), dims.get(b_id)
    if da is None or db is None:
        return None
    shared = [d for d in DECISIVE_LANES
              if isinstance(da.get(d), (int, float)) and isinstance(db.get(d), (int, float))]
    if not shared:
        return None
    w = sum(FEATURE_WEIGHTS[d] for d in shared)
    return sum(FEATURE_WEIGHTS[d] * (db[d] - da[d]) for d in shared) / w


def judge_pick_contradicted(ranked: list, pick_id: str, other_id: str,
                            margin: float = CONTRADICTION_MARGIN) -> bool:
    """True when the decisive lanes favor `other_id` over the judge's pick by
    >= margin. Live run 3 (opus-5-5 claimed): judge picked fable-5-1 while the
    lanes favored opus-5-5 by 0.047 (kb 0.972 vs 0.917, tokenizer 0.45 vs 0.17)."""
    m = decisive_lane_margin(ranked, pick_id, other_id)
    return m is not None and m >= margin
