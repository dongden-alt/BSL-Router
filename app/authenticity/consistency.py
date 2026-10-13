"""Consistency gates for the standards harvester (Phase 4, OPT-IN).

Offline-only helpers: NO network calls at import time or inside any function.
Wire into harvest_model() behind cfg["consistency_v2"] == True.

Budget contract: <= 7 extra paid calls per harvest when the flag is on
  (1 needle paraphrase + up to 6 fact-check probes).
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Deterministic helpers (no LLM, no randomness)
# ---------------------------------------------------------------------------

_PARAPHRASE_TEMPLATE = (
    "Please restate the following question in your own words, then answer it "
    "completely and accurately.\n\nQuestion: {q}\n\nAnswer:"
)


def paraphrase_needle_prompt(prompt: str) -> str:
    """Deterministic offline rewording of a needle prompt.

    Pure function: same input -> same output, no randomness, no LLM call.
    The paraphrase wraps the original question in a fixed alternate framing
    so the model must re-derive rather than echo the probe text.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    q = prompt.strip()
    return _PARAPHRASE_TEMPLATE.format(q=q)


def _tokens(text: str) -> set[str]:
    """Lowercased word tokens."""
    return set(re.findall(r"[a-z0-9']+", (text or "").lower()))


def agreement_score(text_a: str, text_b: str) -> float:
    """Token-set Jaccard similarity in [0.0, 1.0].

    empty/empty -> 1.0  (both absent = vacuous agreement)
    empty/non-empty -> 0.0
    """
    a, b = _tokens(text_a), _tokens(text_b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 1.0


def fact_check_probes(facts: list[dict], max_facts: int = 6) -> list[dict]:
    """Turn harvested known_facts entries into re-ask probe dicts.

    Credit guard: caps at max_facts. Returns [] on empty input.
    Probe shape matches what call_with_backoff expects:
      {"id": "factcheck_N", "prompt": <question>, "max_tokens": 512}
    """
    probes = []
    for i, fact in enumerate((facts or [])[:max_facts]):
        fact_text = (fact.get("fact") or "").strip()
        if not fact_text:
            continue
        probes.append({
            "id": f"factcheck_{i}",
            "prompt": f"Answer the following question concisely and accurately.\n\nQuestion: {fact_text}\n\nAnswer:",
            "max_tokens": 512,
        })
    return probes


def fact_consistency(facts: list[dict], answers: list[str]) -> dict:
    """Score how many harvested facts are re-confirmed by their paired answers.

    For each fact, checks that its needle token appears (case-insensitive)
    in the paired answer string. Paired by list index.

    Returns:
      {"score": float, "consistent": [fact_dict, ...],
       "inconsistent": [{"fact": ..., "needle": ...}, ...],
       "checked": int}
    """
    consistent = []
    inconsistent = []
    checked = 0
    for fact, answer in zip(facts or [], answers or []):
        needle = (fact.get("needle") or "").strip()
        fact_text = (fact.get("fact") or "").strip()
        if not needle or not fact_text:
            continue
        checked += 1
        if needle.lower() in (answer or "").lower():
            consistent.append(fact)
        else:
            inconsistent.append({"fact": fact_text, "needle": needle})
    score = (len(consistent) / checked) if checked else 1.0
    return {
        "score": round(score, 4),
        "consistent": consistent,
        "inconsistent": inconsistent,
        "checked": checked,
    }


# ---------------------------------------------------------------------------
# Cutoff contradiction detector (offline, zero extra calls)
# ---------------------------------------------------------------------------

def detect_cutoff_contradictions(
    cutoff_year: int,
    cutoff_month: int,
    v2_profile: dict,
) -> list[str]:
    """Compare Tier-1 extracted cutoff vs v2 knowledge_bank fields if present.

    Defensive: only compares keys that actually exist in v2_profile.
    Returns human-readable contradiction strings; [] when nothing to compare
    or when all present fields agree.
    """
    contradictions = []
    if not isinstance(v2_profile, dict):
        return contradictions

    kb = v2_profile.get("knowledge_bank")
    if not isinstance(kb, dict):
        return contradictions

    # Knowledge bank may carry a training_cutoff field (year string/int)
    # or a post_cutoff_blind field indicating known post-cutoff events.
    kb_cutoff = kb.get("training_cutoff") or kb.get("cutoff_year")
    if kb_cutoff is not None:
        try:
            kb_year = int(str(kb_cutoff)[:4])
            if kb_year and cutoff_year and kb_year != cutoff_year:
                contradictions.append(
                    f"cutoff_year_mismatch:tier1={cutoff_year}_kb={kb_year}"
                )
        except (ValueError, TypeError):
            pass

    kb_post = kb.get("post_cutoff_blind")
    if kb_post is not None and cutoff_year:
        # If the model claims to know post-cutoff events but Tier-1 says
        # cutoff_year, flag when post_cutoff_blind is explicitly False (i.e.
        # model answered post-cutoff questions = contradiction).
        if isinstance(kb_post, bool) and not kb_post:
            contradictions.append(
                f"post_cutoff_awareness_conflict:tier1_cutoff={cutoff_year}_kb_post_blind=False"
            )

    return contradictions
