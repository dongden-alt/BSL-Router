"""Standards harvester — freeze official TraitMarks from TRUSTED anchors.

A TraitMark is harvested ONCE from a trusted provider (aibox / mintrouter /
codex / antigravity OAuth) and stored as JSON. At test time the suspect is
compared against the STORED standard; the trusted source is never re-called.

Fail-open per layer: a failed probe leaves that layer at defaults and records
the gap in meta.harvest_gaps. A single model's failure never aborts a batch.

Test seams: monkeypatch `_make_client` (return an httpx.AsyncClient backed by
MockTransport) and `STANDARDS_DIR` (tmp path).
"""
import asyncio
import datetime
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional
from dataclasses import asdict

import httpx

from .fingerprint import extract_features
from .probes import (TIER1_PROBES, TIER2_PROBES, HARVEST_NEEDLE_PROBES,
                     LOGPROB_PROBES, run_boundary_probes, run_stability_probe,
                     run_timing_probe, run_probe)
from .scope import detect_family, normalize
from .traitmark import TraitMark

logger = logging.getLogger(__name__)

# Test seam: main.py reads STANDARDS_DIR at request time so monkeypatching it
# here propagates. Never resolve config via a bare "config.yaml".
STANDARDS_DIR = Path(__file__).parent / "reference" / "standards"


def _make_client() -> httpx.AsyncClient:
    """Test seam: tests monkeypatch this to return a MockTransport client."""
    return httpx.AsyncClient()


# ── T2.5 non-coding probe battery ────────────────────────────────────────────
# Outputs are graded by the judge model on fixed axes. Needles let tests
# distinguish each probe deterministically.
NONCODING_PROBES = [
    {
        "id": "creative_story",
        "axis": "creative",
        "prompt": (
            "Write a 4-sentence micro-story about a lighthouse keeper who is "
            "afraid of the dark. No title, no preamble."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "creative_metaphor",
        "axis": "creative",
        "prompt": (
            "Describe memory in three sentences. Each sentence must contain "
            "exactly one metaphor drawn from weather — no literal statements, "
            "no preamble, no title."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "eq_presence",
        "axis": "eq",
        "prompt": (
            "A friend just told you they got passed over for a promotion they'd "
            "worked toward for 2 years. Respond as that friend needs to hear — "
            "not advice, just presence. 3-4 sentences."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "humor_explain",
        "axis": "humor",
        "prompt": (
            "Explain why a database index is like a library card catalog, but "
            "make it gently funny. Under 150 words."
        ),
        "max_tokens": 4096,
    },
    {
        "id": "design_critique",
        "axis": "design",
        "prompt": (
            "Critique this landing page: a hero with a stock photo of a "
            "handshake, three generic feature cards, and a big blue 'Submit' "
            "button. Be specific about what is weak and what you'd change."
        ),
        "max_tokens": 4096,
    },
]

JUDGE_AXES = ("creative", "eq", "humor", "design")

JUDGE_RUBRIC = (
    "You are grading a language model's NON-CODING output quality. Score each "
    "axis from 1 to 10 and give ONE sentence of justification.\n"
    "Axes:\n"
    "- creative: originality, imagery, control of voice (low = generic/clichéd)\n"
    "- eq: empathy, perspective-taking, emotional attunement (low = robotic/advice-y)\n"
    "- humor: does the humor land without being forced (low = flat/trying too hard)\n"
    "- design: taste, specificity, critique quality (low = vague/generic praise)\n"
    "Return STRICT JSON only, no prose, no markdown fences, exactly this shape:\n"
    '{"creative":{"score":int,"why":str},"eq":{"score":int,"why":str},'
    '"humor":{"score":int,"why":str},"design":{"score":int,"why":str}}'
)


# ── Judge ────────────────────────────────────────────────────────────────────

def _strip_json_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Leading/trailing prose around a JSON object
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    return t


async def judge_scores(
    judge_endpoint: str,
    judge_key: str,
    judge_model: str,
    probe_outputs: list,
    timeout_s: int = 60,
) -> dict:
    """Grade non-coding probe outputs with the judge model. NEVER raises.

    probe_outputs: list of {"axis", "id", "content"}.
    Returns {axis: {"score": int, "why": str}}; on any failure every axis is
    {"score": 0, "why": "judge_error"}.
    """
    err = {a: {"score": 0, "why": "judge_error"} for a in JUDGE_AXES}
    if not (judge_endpoint and judge_key and judge_model) or not probe_outputs:
        return err

    numbered = "\n\n".join(
        f"[{i}] axis={o.get('axis','?')} id={o.get('id','?')}\n{o.get('content','')}"
        for i, o in enumerate(probe_outputs)
    )
    judge_prompt = (
        f"{JUDGE_RUBRIC}\n\nHere are the model's outputs to grade:\n\n{numbered}\n\n"
        "Return ONLY the JSON object."
    )

    base = judge_endpoint.rstrip("/")
    url = f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"
    try:
        async with _make_client() as client:
            resp = await client.post(
                url,
                json={
                    "model": judge_model,
                    "messages": [
                        {"role": "system", "content": "You output only valid JSON."},
                        {"role": "user", "content": judge_prompt},
                    ],
                    "max_tokens": 3072,
                    "stream": False,
                },
                headers={
                    "Authorization": f"Bearer {judge_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_s,
            )
        if resp.status_code != 200:
            return err
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        parsed = json.loads(_strip_json_fences(content))

        out = {}
        for axis in JUDGE_AXES:
            node = parsed.get(axis) or {}
            try:
                score = int(node.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(10, score))
            why = str(node.get("why", ""))[:400]
            out[axis] = {"score": score, "why": why}
        return out
    except Exception as exc:
        logger.warning("judge_scores failed: %s", exc)
        return err


# ── Harvest ──────────────────────────────────────────────────────────────────

def _parse_needle_entries(text: str) -> list:
    """Parse anchor self-reported needle text into {fact, needle} entries.

    Expected line format: 'detail ||| event' (known-edge) or a plain line
    (post-cutoff). Each entry carries fact (question text) + needle (the
    distinctive token a correct answer must contain)."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("0123456789.-) ").strip()
        if not line or len(line) < 8:
            continue
        if "|||" in line:
            needle, _, fact = line.partition("|||")
            fact, needle = fact.strip(), needle.strip()
        else:
            fact, needle = line, ""
        if fact and fact not in [e["fact"] for e in out]:
            out.append({"fact": fact, "needle": (needle or fact)[:80]})
    return out[:8]


def _family_ref(model_id: str) -> dict:
    """Minimal family reference for extract_features (keywords)."""
    fam = detect_family(model_id) or ""
    # extract_features only needs self/impostor keyword lists; pull from a
    # families.json if present, else synthesize from the detected family.
    families_path = Path(__file__).parent / "reference" / "families.json"
    if families_path.exists():
        try:
            families = json.loads(families_path.read_text(encoding="utf-8"))
            if fam in families:
                return families[fam]
        except Exception:
            pass
    return {
        "family": fam,
        "self_keywords": [fam] if fam else [],
        "impostor_keywords": [k for k in ("glm", "kimi", "qwen", "deepseek",
                                          "claude", "gpt", "gemini", "mimo",
                                          "hunyuan") if k != fam],
    }


async def harvest_model(
    endpoint: str,
    api_key: str,
    model_id: str,
    *,
    judge_endpoint: Optional[str] = None,
    judge_key: Optional[str] = None,
    judge_model: Optional[str] = None,
    include_noncoding: bool = True,
    source_tag: str = "",
    config: Optional[dict] = None,
) -> TraitMark:
    """Probe a trusted anchor and build its TraitMark. NEVER raises."""
    cfg = config or {}
    probe_timeout = int(cfg.get("probe_timeout_s", 90))
    total_timeout = int(cfg.get("total_timeout_s", 300))
    max_tier = int(cfg.get("max_tier", 2))

    tm = TraitMark()
    tm.meta.model_id = model_id
    tm.meta.family = detect_family(model_id) or ""
    tm.meta.sub_model = normalize(model_id)
    tm.meta.tier = "canonical"
    tm.meta.collected_at = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    tm.meta.collected_from = [endpoint]
    tm.meta.source_tag = (source_tag or "").strip()
    if not hasattr(tm.meta, "harvest_gaps"):
        # MetaLayer gained harvest_gaps; from_dict filters unknown fields, so
        # setting it here is forward/backward compatible either way.
        try:
            tm.meta.harvest_gaps = []
        except Exception:
            pass

    gaps = []
    t0 = time.monotonic()
    family_ref = _family_ref(model_id)
    noncoding_outputs = []
    needle_known_text = ""
    needle_post_text = ""
    logprob_profile = {}

    try:
        async with _make_client() as client:

            async def _probe(probe, tier):
                if (time.monotonic() - t0) > total_timeout:
                    gaps.append(f"timeout_before:{probe['id']}")
                    return None
                r = await run_probe(client, endpoint, api_key, model_id,
                                    probe, probe_timeout)
                r.tier = tier
                if r.error:
                    gaps.append(f"probe_error:{probe['id']}")
                return r

            async def _probe_lp(probe):
                if (time.monotonic() - t0) > total_timeout:
                    gaps.append(f"timeout_before:{probe['id']}")
                    return None
                r = await run_probe(client, endpoint, api_key, model_id, probe,
                                    probe_timeout,
                                    extra={"logprobs": True, "top_logprobs": 5,
                                           "temperature": 0})
                r.tier = 2
                return r

            # Tier 1 + Tier 2 (parallel): reasoning probes take ~20s each, so
            # sequential execution blows the total battery timeout. Gather runs
            # them concurrently over the shared client.
            tier_jobs = [(_probe(p, 1)) for p in TIER1_PROBES]
            if max_tier >= 2:
                tier_jobs += [(_probe(p, 2)) for p in TIER2_PROBES]
            # Version-discrimination capture: needles (self-reported) + logprobs.
            tier_jobs += [_probe(p, 2) for p in HARVEST_NEEDLE_PROBES]
            tier_jobs += [_probe_lp(p) for p in LOGPROB_PROBES]
            tier_results = [r for r in await asyncio.gather(*tier_jobs) if r]

            # Split needle + logprob results back out by probe id.
            for r in tier_results:
                if r.probe_id == "needle_known_edge" and not r.error:
                    needle_known_text = r.content or ""
                elif r.probe_id == "needle_post_cutoff" and not r.error:
                    needle_post_text = r.content or ""
                elif r.probe_id.startswith("lp_tok_") and r.logprobs:
                    first = (r.logprobs.get("content") or [{}])[0]
                    logprob_profile[r.probe_id] = {
                        "logprob": first.get("logprob"),
                        "top": {t.get("token"): t.get("logprob")
                                for t in (first.get("top_logprobs") or [])},
                    }

            # Tier 2.5 non-coding (parallel; outputs kept for the judge)
            if include_noncoding:
                nc_results = await asyncio.gather(*[_probe(p, 3) for p in NONCODING_PROBES])
                for probe, r in zip(NONCODING_PROBES, nc_results):
                    if r and not r.error:
                        noncoding_outputs.append(
                            {"axis": probe["axis"], "id": probe["id"],
                             "content": r.content})

            # V6a/V6b/V7 cascade captures (harvest is offline: L1/L2/L3 ALL
            # run — baselines must be complete, no abstain trigger needed).
            # Router-only: every helper goes through the endpoint key above.
            boundary_signature = {}
            stability_profile = {}
            timing_profile = {}
            try:
                if (time.monotonic() - t0) < total_timeout:
                    boundary_signature = await run_boundary_probes(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"boundary_capture_error:{str(exc)[:120]}")
            try:
                if (time.monotonic() - t0) < total_timeout:
                    stability_profile = await run_stability_probe(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"stability_capture_error:{str(exc)[:120]}")
            try:
                if (time.monotonic() - t0) < total_timeout:
                    timing_profile = await run_timing_probe(
                        client, endpoint, api_key, model_id, probe_timeout)
            except Exception as exc:
                gaps.append(f"timing_capture_error:{str(exc)[:120]}")
    except Exception as exc:
        gaps.append(f"probe_batch_error:{str(exc)[:120]}")

    # Populate layers from extracted features (best-effort).
    try:
        feats = extract_features(tier_results, family_ref)
        k = tm.knowledge
        k.cutoff_year = int(getattr(feats, "cutoff_year", 0) or 0)
        k.cutoff_month = int(getattr(feats, "cutoff_month", 0) or 0)
        k.capability_boundary = {
            "correct": int(getattr(feats, "capability_correct", 0) or 0)}
        # V1 needles: self-reported cutoff-edge knowns + admitted post-cutoff unknowns.
        k.known_facts = _parse_needle_entries(needle_known_text)
        k.post_cutoff_unknown = _parse_needle_entries(needle_post_text)
        # V5 logprob tail (weight-level). Captured via the ROUTER endpoint —
        # the router passes logprobs through its SSE buffer, so no upstream
        # bypass or provider keys are needed (harvest HARD RULE: router only).
        tm.structural.logprob_profile = logprob_profile
        if not tm.structural.logprob_profile:
            gaps.append("logprobs:unavailable")
        if not k.known_facts and not k.post_cutoff_unknown:
            gaps.append("needles:no_self_report")

        # V6a/V6b/V7 cascade profiles into the structural layer. The
        # StructuralLayer dataclass (read-only traitmark.py) has no fields for
        # these, so they nest inside tokenizer_signature, whose dict shape the
        # matcher already reads through (V5 logprob_profile precedent).
        _struct = tm.structural.tokenizer_signature
        if isinstance(boundary_signature, dict) and boundary_signature:
            _struct["boundary_signature"] = boundary_signature
        if isinstance(stability_profile, dict) and stability_profile.get("mode_hash"):
            _struct["stability_profile"] = stability_profile
        if isinstance(timing_profile, dict) and timing_profile.get("chunk_count"):
            _struct["timing_profile"] = timing_profile
        if not _struct.get("boundary_signature"):
            gaps.append("boundary:unavailable")
        if not _struct.get("stability_profile"):
            gaps.append("stability:unavailable")
        if not _struct.get("timing_profile"):
            gaps.append("timing:unavailable")

        s = tm.style
        s.bullet_style = getattr(feats, "bullet_style", "") or ""
        s.header_depth = int(getattr(feats, "header_depth", 0) or 0)
        s.avg_sentence_length = float(getattr(feats, "avg_sentence_length", 0.0) or 0.0)
        s.cjk_ratio = float(getattr(feats, "cjk_ratio", 0.0) or 0.0)
        s.default_response_length = int(getattr(feats, "response_length", 0) or 0)

        r = tm.reasoning
        r.emits_thinking = bool(
            any((getattr(pr, "thinking", "") or "") for pr in tier_results))

        idn = tm.identity
        idn.self_claim_model = getattr(feats, "claimed_model", "") or ""
        idn.self_claim_org = getattr(feats, "claimed_org", "") or ""
        idn.self_keywords = list(family_ref.get("self_keywords", []))
        idn.impostor_keywords = list(family_ref.get("impostor_keywords", []))
    except Exception as exc:
        gaps.append(f"feature_extract_error:{str(exc)[:120]}")

    # Judge the non-coding outputs (only with judge creds + outputs).
    if include_noncoding and noncoding_outputs and \
            judge_endpoint and judge_key and judge_model:
        scores = await judge_scores(judge_endpoint, judge_key, judge_model,
                                    noncoding_outputs)
        for axis in JUDGE_AXES:
            setattr(tm.noncoding, axis, scores.get(
                axis, {"score": 0, "why": "judge_error"}))
    elif include_noncoding:
        gaps.append("noncoding:no_judge_or_outputs")

    try:
        tm.meta.judged_by = judge_model if (judge_endpoint and judge_key and judge_model) else ""
        tm.meta.harvest_gaps = gaps
    except Exception:
        pass
    return tm


# ── Persistence ──────────────────────────────────────────────────────────────

def _standard_filename(tm: TraitMark) -> str:
    fam = re.sub(r"[^a-z0-9_-]+", "-", (tm.meta.family or "unknown").lower())
    sub = re.sub(r"[^a-z0-9_-]+", "-", (tm.meta.sub_model or tm.meta.model_id
                                        or "model").lower())
    src = re.sub(r"[^a-z0-9_-]+", "-", (tm.meta.source_tag or "").lower())
    return f"{fam}_{sub}_{src}.json" if src else f"{fam}_{sub}.json"


def save_standard(tm: TraitMark, standards_dir) -> Path:
    """Atomic write: temp file on same filesystem + os.replace."""
    d = Path(standards_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / _standard_filename(tm)
    fd, tmp_path = tempfile.mkstemp(dir=str(d), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tm.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def _layer_nonempty(layer) -> bool:
    """True if a dataclass layer carries ANY non-default behavioral signal."""
    try:
        d = asdict(layer)
    except Exception:
        return False
    for v in d.values():
        if isinstance(v, bool):
            if v:
                return True
        elif isinstance(v, (int, float)):
            if v not in (0, 0.0):
                return True
        elif isinstance(v, str):
            if v.strip():
                return True
        elif isinstance(v, (list, dict)):
            if v:
                return True
    return False


def standard_quality(tm: TraitMark) -> dict:
    """Judge-independent quality grade of a frozen standard.

    full          -> judge-graded T2.5 noncoding battery captured (no judge gap) AND
                     >=4 behavioral layers populated. High-confidence verification.
    family_only   -> some signal but the noncoding battery is missing/hollow
                     (the 'no_judge_or_outputs' gap) -> family classification only.
    hollow        -> almost no behavioral signal captured; do NOT trust Tier-2.
    """
    gaps = list(getattr(tm.meta, "harvest_gaps", []) or [])
    layers = {
        "identity": _layer_nonempty(tm.identity),
        "knowledge": _layer_nonempty(tm.knowledge),
        "reasoning": _layer_nonempty(tm.reasoning),
        "style": _layer_nonempty(tm.style),
        "lexical": _layer_nonempty(tm.lexical),
        "noncoding": _layer_nonempty(tm.noncoding),
        "structural": _layer_nonempty(tm.structural),
    }
    populated = sum(1 for v in layers.values() if v)
    judge_gap = any("no_judge" in g for g in gaps)
    noncoding_ok = layers["noncoding"] and not judge_gap
    if populated < 3:
        grade = "hollow"
    elif noncoding_ok and populated >= 4:
        grade = "full"
    else:
        grade = "family_only"
    return {
        "grade": grade,
        "populated_layers": populated,
        "layers": layers,
        "judge_gap": judge_gap,
        "high_confidence": grade == "full",
        "gaps": gaps,
    }


# Judge suggestion: a judge must be a DIFFERENT family from the suspect, else
# creative/EQ/design scores are inflated by family resemblance. Default = claude-opus-5-5.
# Generic judge defaults — name the FAMILY only, never a provider id. The judge
# is chosen from the user's own enabled models at runtime; these are fallbacks.
# (The old mintrouter/codex ids leaked private infrastructure into a public repo.)
JUDGE_DEFAULT = "claude"
JUDGE_SUGGESTIONS = {
    "claude":   "gpt",
    "gpt":      "claude",
    "gemini":   "claude",
    "glm":      "claude",
    "kimi":     "claude",
    "deepseek": "claude",
    "qwen":     "claude",
    "mimo":     "claude",
}

# Vendor display names for the public testable-models database (family -> vendor).
_FAMILY_VENDOR = {
    "claude": "Anthropic", "gpt": "OpenAI", "gemini": "Google", "glm": "Zhipu",
    "qwen": "Alibaba", "deepseek": "DeepSeek", "kimi": "Moonshot", "mimo": "Xiaomi",
}


def suggest_judge(family: str) -> str:
    """Recommended judge for a suspect of `family` (always a different family)."""
    return JUDGE_SUGGESTIONS.get((family or "").lower(), JUDGE_DEFAULT)


def list_standards(standards_dir) -> list:
    """Summaries for the admin UI. Never raises."""
    out = []
    d = Path(standards_dir)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tm = TraitMark.from_dict(data)
            q = standard_quality(tm)
            out.append({
                "family": tm.meta.family,
                "sub_model": tm.meta.sub_model,
                "collected_at": tm.meta.collected_at,
                "tier": tm.meta.tier,
                "stale": tm.is_stale(),
                "file": path.name,
                "gaps": getattr(tm.meta, "harvest_gaps", []),
                "judged_by": getattr(tm.meta, "judged_by", ""),
                "source_tag": getattr(tm.meta, "source_tag", ""),
                "suggested_judge": suggest_judge(tm.meta.family),
                "quality": q["grade"],
                "populated_layers": q["populated_layers"],
                "high_confidence": q["high_confidence"],
            })
        except Exception:
            continue
    return out


def list_testable_standards(standards_dir) -> dict:
    """Public testable-models database, grouped by vendor.

    Only CANONICAL standards are listed (source_tag empty) — provider-tagged
    local references are private infrastructure and never surface in the
    public list. Each entry's sub_model is display-normalised (family prefix
    stripped) so the UI renders "claude-fable-5-1" under "Anthropic".
    """
    standards = list_standards(standards_dir)
    # Public/private split: a standard is PUBLIC only if its sub_model carries no
    # provider token ANYWHERE — slash-prefixed ('codex/gpt-5.5'), dash-prefixed
    # ('mintrouter-claude-...'), or bare ('workbuddy'). Provider-tagged local
    # references are private infrastructure and never surface in the public
    # testable list — matching the gitignore whitelist.
    import re as _re
    _PRIV = _re.compile(r'(^|[/\-_])(mintrouter|vsllm-o|vsllm-r|pix4k|a6api|sharellm|'
                        r'workbuddy|zai-web\d|chat2api|antigravity|arena-web|'
                        r'deepseek-web|qwen-web|mimo-web|codex|ds|zhipu)([/\-_]|$)', _re.I)
    canonical = [s for s in standards
                 if not (s.get("source_tag") or "").strip()
                 and not _PRIV.search(s.get("sub_model", ""))]
    full = [s for s in canonical if s.get("high_confidence")]
    partial = [s for s in canonical if not s.get("high_confidence")]

    def _display(s):
        sub = s.get("sub_model", "")
        fam = s.get("family", "")
        # strip family prefix: 'claude-claude-fable-5-1' -> 'fable-5-1' only if doubled
        if sub.lower().startswith(fam.lower() + "-"):
            sub = sub[len(fam) + 1:]
        return sub

    def _group(entries, ok):
        groups = {}
        for s in entries:
            fam = s.get("family", "")
            vendor = _FAMILY_VENDOR.get(fam.lower(), fam.title())
            groups.setdefault(vendor, []).append({
                "model": _display(s),
                "quality": s.get("quality", ""),
                "confidence": "high" if ok else "family-only",
                "suggested_judge": s.get("suggested_judge", JUDGE_DEFAULT),
            })
        for vendor in groups:
            groups[vendor].sort(key=lambda x: x["model"])
        return dict(sorted(groups.items()))

    return {
        "vendors": _group(full, True),
        "family_only_vendors": _group(partial, False),
        "count_full": len(full),
        "count_family_only": len(partial),
    }


def delete_standard(standards_dir, family: str, sub_model: str, source_tag: str = "") -> bool:
    d = Path(standards_dir)
    fam = re.sub(r"[^a-z0-9_-]+", "-", (family or "").lower())
    sub = re.sub(r"[^a-z0-9_-]+", "-", (sub_model or "").lower())
    src = re.sub(r"[^a-z0-9_-]+", "-", (source_tag or "").lower())
    target = d / (f"{fam}_{sub}_{src}.json" if src else f"{fam}_{sub}.json")
    if target.exists():
        target.unlink()
        return True
    return False
