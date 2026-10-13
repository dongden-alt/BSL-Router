"""Offline pairwise discrimination index over harvested authenticity standards.

Phase 0 / Phase 1 of the adaptive authenticity protocol:
  - builds, from data already on disk, a machine-readable index of which
    non-distillable lanes discriminate every pair of standards,
  - flags corrupted harvest entries (concatenation artifacts, empty needles,
    duplicate needles, unreliable self-claims),
  - validates that the canonical models are pairwise-separable from every
    other standard using only existing data.

Zero API calls. Reads only app/authenticity/reference/standards/*.json.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
STANDARDS_DIR = _REPO_ROOT / "app" / "authenticity" / "reference" / "standards"
DEFAULT_INDEX_PATH = _REPO_ROOT / ".brain" / "scratch" / "discrimination_index.json"

LANES = ("tokenizer", "refusal", "cultural", "cutoff", "formatting")

PAIR_TYPE_SAME_FAMILY = "same_family"
PAIR_TYPE_DISTILLATION_SUSPECT = "distillation_suspect"
PAIR_TYPE_CROSS_FAMILY = "cross_family"

# Families with documented distillation exposure toward frontier families.
_DISTILLATION_STUDENTS = {
    "deepseek", "qwen", "glm", "zhipu", "kimi", "moonshot", "minimax", "yi",
}
_DISTILLATION_TEACHERS = {"gpt", "openai", "claude", "anthropic", "gemini", "google"}

# Lane weights per pair type: same-family pairs share tokenizer/refusal/cultural
# priors, so only cutoff + formatting carry weight there; distillation pairs
# must be separated on non-distillable lanes only.
LANE_WEIGHTS = {
    PAIR_TYPE_CROSS_FAMILY: {
        "tokenizer": 1.0, "refusal": 1.0, "cultural": 0.7, "cutoff": 0.5, "formatting": 0.4,
    },
    PAIR_TYPE_DISTILLATION_SUSPECT: {
        "tokenizer": 1.0, "refusal": 1.0, "cultural": 1.0, "cutoff": 0.6, "formatting": 0.3,
    },
    PAIR_TYPE_SAME_FAMILY: {
        "tokenizer": 0.0, "refusal": 0.2, "cultural": 0.2, "cutoff": 1.0, "formatting": 0.8,
    },
}

SEPARABILITY_THRESHOLD = 1.0
THIN_THRESHOLD = 0.5

CANONICAL_MODELS = ("claude-opus-5-5", "gpt-5.6-sol", "glm-5.3")

# Per-axis scale for formatting drift: a drift counts when |a-b| >= scale.
_FMT_KEYS = (
    "header_depth", "avg_sentence_length", "sentence_length_variance",
    "cjk_ratio", "emoji_usage", "markdown_density", "default_response_length",
)
_FMT_SCALE = {
    "header_depth": 1.0,
    "avg_sentence_length": 5.0,
    "sentence_length_variance": 5.0,
    "cjk_ratio": 0.10,
    "emoji_usage": 0.05,
    "markdown_density": 0.10,
    "default_response_length": 100.0,
}

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class LaneResult:
    status: str  # "discriminates" | "similar" | "unknown"
    evidence: list = field(default_factory=list)


# ---------------------------------------------------------------- loading

def load_standards(standards_dir=None):
    """Load every standard file, keyed by meta.model_id (filename fallback)."""
    root = Path(standards_dir) if standards_dir else STANDARDS_DIR
    out = {}
    for p in sorted(root.glob("*.json")):
        try:
            std = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(std, dict) or not isinstance(std.get("meta"), dict):
            continue
        model_id = (std["meta"].get("model_id") or "").strip() or p.stem
        std["_file"] = p.name
        out[model_id] = std
    return out


# ---------------------------------------------------------------- fingerprints

def _as_num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _needles(entries):
    return sorted({
        (e.get("needle") or "").strip().lower()
        for e in (entries or [])
        if isinstance(e, dict) and (e.get("needle") or "").strip()
    })


def extract_fingerprint(std):
    """Per-lane comparable fingerprints + coverage flags for one standard."""
    structural = std.get("structural") or {}
    tok = structural.get("tokenizer_signature") or {}
    boundary = tok.get("boundary_signature") or {}
    stability = tok.get("stability_profile") or {}
    style = std.get("style") or {}
    lexical = std.get("lexical") or {}
    noncoding = std.get("noncoding") or {}
    knowledge = std.get("knowledge") or {}
    identity = std.get("identity") or {}

    nc_vec = []
    for axis in ("creative", "eq", "humor", "design"):
        nc_vec.append(_as_num((noncoding.get(axis) or {}).get("score")))

    fmt_vec = [_as_num(style.get(k)) for k in _FMT_KEYS]

    refusal_style = (style.get("refusal_style") or "").strip().lower()
    refusal_identity = (identity.get("refusal_identity") or "").strip().lower()
    # Refusal lane: harvest.py stores the probe signature in the dedicated
    # top-level "refusal" section (axes / delta_signature / templates), NOT in
    # style.refusal_style / identity.refusal_identity -- those legacy text slots
    # are empty in every harvested standard (0/59 coverage was a reader bug,
    # not missing data). Read both so coverage and comparison reflect reality.
    refusal_layer = std.get("refusal") or {}
    refusal_axes = refusal_layer.get("axes") or {}
    refusal_delta = refusal_layer.get("delta_signature") or {}

    return {
        "tokenizer": {
            "covered": bool(boundary) or bool(stability.get("mode_hash")),
            "boundary": boundary,
            "mode_hash": stability.get("mode_hash"),
        },
        "refusal": {
            "covered": bool(refusal_axes) or bool(refusal_style) or bool(refusal_identity),
            "style_text": refusal_style,
            "identity_text": refusal_identity,
            "axes": refusal_axes,
            "delta": refusal_delta,
        },
        "cultural": {
            "covered": sum(v is not None for v in nc_vec) >= 2,
            "noncoding_vec": nc_vec,
            "cjk_ratio": _as_num(style.get("cjk_ratio")),
        },
        "cutoff": {
            "covered": isinstance(knowledge.get("cutoff_year"), int),
            "year": knowledge.get("cutoff_year"),
            "month": knowledge.get("cutoff_month"),
            "known_needles": _needles(knowledge.get("known_facts")),
            "unknown_needles": _needles(knowledge.get("post_cutoff_unknown")),
        },
        "formatting": {
            "covered": sum(v is not None for v in fmt_vec) >= 3,
            "vec": fmt_vec,
            "bullet": (style.get("bullet_style") or "").strip(),
            "slop": _as_num(lexical.get("slop_score")),
            "vocab": _as_num(lexical.get("vocabulary_complexity")),
        },
    }


# ---------------------------------------------------------------- lane comparators

def _words(text):
    return set(_WORD_RE.findall(text or ""))


def _jaccard(a, b):
    if not a or not b:
        return None
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else None


def cmp_tokenizer(fa, fb):
    if not (fa["covered"] and fb["covered"]):
        missing = [name for name, f in (("a", fa), ("b", fb)) if not f["covered"]]
        return LaneResult("unknown", [f"tokenizer_signature missing on side(s): {','.join(missing)}"])
    diffs = []
    for k in sorted(set(fa["boundary"]) | set(fb["boundary"])):
        va, vb = fa["boundary"].get(k), fb["boundary"].get(k)
        if va != vb:
            diffs.append(f"boundary.{k}: {va!r} != {vb!r}")
    if fa["mode_hash"] and fb["mode_hash"] and fa["mode_hash"] != fb["mode_hash"]:
        diffs.append("stability mode_hash differs")
    return LaneResult("discriminates" if diffs else "similar", diffs)


def cmp_refusal(fa, fb):
    if not (fa["covered"] and fb["covered"]):
        return LaneResult("unknown", ["refusal data missing on one side"])
    evidence = []
    # Signal 1: verdict-map mismatch on shared (culture, lang) probe cells.
    # "error" verdicts are probe failures, not behaviour -- never evidence.
    axes_a, axes_b = fa.get("axes") or {}, fb.get("axes") or {}
    for culture in sorted(set(axes_a) & set(axes_b)):
        langs_a = axes_a.get(culture) or {}
        langs_b = axes_b.get(culture) or {}
        if not isinstance(langs_a, dict) or not isinstance(langs_b, dict):
            continue
        for lang in sorted(set(langs_a) & set(langs_b)):
            va = (langs_a.get(lang) or {}).get("verdict")
            vb = (langs_b.get(lang) or {}).get("verdict")
            if not va or not vb or va == "error" or vb == "error":
                continue
            if va != vb:
                evidence.append(f"verdict[{culture}/{lang}]: {va!r} != {vb!r}")
    # Signal 2: delta-signature numeric gap (alignment drift per culture).
    delta_a, delta_b = fa.get("delta") or {}, fb.get("delta") or {}
    for culture in sorted(set(delta_a) & set(delta_b)):
        da, db = _as_num(delta_a.get(culture)), _as_num(delta_b.get(culture))
        if da is None or db is None:
            continue
        if abs(da - db) >= 0.5:
            evidence.append(f"delta[{culture}]: {da} vs {db} (gap >= 0.50)")
    # Signal 3: legacy style/identity text fallback (pre-refusal-layer data).
    for label in ("style_text", "identity_text"):
        ta, tb = fa.get(label) or "", fb.get(label) or ""
        if not ta or not tb:
            continue
        j = _jaccard(_words(ta), _words(tb))
        if j is not None and j < 0.6:
            evidence.append(f"refusal {label} word-jaccard {j:.2f} < 0.60")
    return LaneResult("discriminates" if evidence else "similar", evidence)


def cmp_cultural(fa, fb):
    if not (fa["covered"] and fb["covered"]):
        return LaneResult("unknown", ["noncoding axes missing on one side"])
    axes = ("creative", "eq", "humor", "design")
    diffs = []
    shared = 0
    abs_sum = 0.0
    for i, axis in enumerate(axes):
        va, vb = fa["noncoding_vec"][i], fb["noncoding_vec"][i]
        if va is None or vb is None:
            continue
        shared += 1
        abs_sum += abs(va - vb)
        if abs(va - vb) >= 3:
            diffs.append(f"noncoding.{axis}: {va} vs {vb}")
    if shared >= 2 and abs_sum / shared >= 1.5:
        diffs.append(f"mean abs noncoding diff {abs_sum / shared:.2f} >= 1.50")
    ca, cb = fa["cjk_ratio"], fb["cjk_ratio"]
    if ca is not None and cb is not None and abs(ca - cb) >= 0.15:
        diffs.append(f"cjk_ratio gap {abs(ca - cb):.2f} >= 0.15")
    return LaneResult("discriminates" if diffs else "similar", diffs)


def cmp_cutoff(fa, fb):
    if not (fa["covered"] and fb["covered"]):
        return LaneResult("unknown", ["cutoff year missing on one side"])
    ya, yb = fa["year"], fb["year"]
    ma, mb = fa.get("month"), fb.get("month")
    if ya != yb or (ma is not None and mb is not None and ma != mb):
        return LaneResult("discriminates", [f"cutoff date differs: {ya}-{ma} vs {yb}-{mb}"])
    evidence = []
    for label in ("known_needles", "unknown_needles"):
        sa, sb = set(fa[label]), set(fb[label])
        j = _jaccard(sa, sb)
        if j is not None and j < 0.5:
            evidence.append(f"{label} jaccard {j:.2f} < 0.50")
    if evidence:
        return LaneResult("discriminates", evidence)
    return LaneResult("similar", ["identical cutoff date; needle sets not separable"])


def cmp_formatting(fa, fb):
    if not (fa["covered"] and fb["covered"]):
        return LaneResult("unknown", ["style vector too sparse on one side"])
    drifted = []
    for i, key in enumerate(_FMT_KEYS):
        va, vb = fa["vec"][i], fb["vec"][i]
        if va is None or vb is None:
            continue
        if abs(va - vb) >= _FMT_SCALE[key]:
            drifted.append(f"{key}: {va} vs {vb}")
    if fa["bullet"] and fb["bullet"] and fa["bullet"] != fb["bullet"]:
        drifted.append(f"bullet_style: {fa['bullet']!r} vs {fb['bullet']!r}")
    if fa["slop"] is not None and fb["slop"] is not None and abs(fa["slop"] - fb["slop"]) >= 0.15:
        drifted.append(f"slop_score: {fa['slop']} vs {fb['slop']}")
    status = "discriminates" if len(drifted) >= 2 else "similar"
    return LaneResult(status, drifted)


LANE_COMPARATORS = {
    "tokenizer": cmp_tokenizer,
    "refusal": cmp_refusal,
    "cultural": cmp_cultural,
    "cutoff": cmp_cutoff,
    "formatting": cmp_formatting,
}


# ---------------------------------------------------------------- pair logic

def classify_pair(family_a, family_b):
    fa, fb = (family_a or "").lower(), (family_b or "").lower()
    if fa and fa == fb:
        return PAIR_TYPE_SAME_FAMILY
    if (fa in _DISTILLATION_STUDENTS and fb in _DISTILLATION_TEACHERS) or (
        fb in _DISTILLATION_STUDENTS and fa in _DISTILLATION_TEACHERS
    ):
        return PAIR_TYPE_DISTILLATION_SUSPECT
    return PAIR_TYPE_CROSS_FAMILY


def compare_pair(std_a, std_b):
    """Full lane-by-lane comparison of two standards."""
    fa, fb = extract_fingerprint(std_a), extract_fingerprint(std_b)
    family_a = (std_a.get("meta") or {}).get("family") or ""
    family_b = (std_b.get("meta") or {}).get("family") or ""
    pair_type = classify_pair(family_a, family_b)
    weights = LANE_WEIGHTS[pair_type]
    lanes = {}
    score = 0.0
    for lane in LANES:
        res = LANE_COMPARATORS[lane](fa[lane], fb[lane])
        lanes[lane] = {"status": res.status, "evidence": res.evidence}
        if res.status == "discriminates":
            score += weights[lane]
    if score >= SEPARABILITY_THRESHOLD:
        sep = "separable"
    elif score >= THIN_THRESHOLD:
        sep = "thin"
    else:
        sep = "inseparable_with_current_data"
    return {
        "pair_type": pair_type,
        "lanes": lanes,
        "strong_lanes": [l for l in LANES if lanes[l]["status"] == "discriminates" and weights[l] >= 0.7],
        "separability_score": round(score, 3),
        "separability": sep,
    }


# ---------------------------------------------------------------- corruption

def detect_corruption(std):
    flags = []
    knowledge = std.get("knowledge") or {}
    identity = std.get("identity") or {}
    meta = std.get("meta") or {}
    for section in ("known_facts", "post_cutoff_unknown"):
        entries = knowledge.get(section) or []
        needles = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            fact = (e.get("fact") or "").strip()
            needle = (e.get("needle") or "").strip()
            if "|||" in fact or "|||" in needle:
                flags.append(f"{section}: concatenation artifact ('|||')")
            if not fact or not needle:
                flags.append(f"{section}: empty fact/needle entry")
            if needle:
                needles.append(needle.lower())
        dupes = sorted({n for n in needles if needles.count(n) > 1})
        if dupes:
            flags.append(f"{section}: duplicate needles {dupes}")
        # cross-entry contamination: needle of one entry embedded in another's fact
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            fact_l = (e.get("fact") or "").lower()
            for j, n in enumerate(needles):
                if i != j and n and n in fact_l:
                    flags.append(f"{section}: needle of entry {j} embedded in fact of entry {i}")
                    break
    self_claim = (identity.get("self_claim_model") or "").strip().lower()
    family = (meta.get("family") or "").strip().lower()
    model_id = (meta.get("model_id") or "").strip().lower()
    if self_claim and family and family not in self_claim and model_id.split("-")[0] not in self_claim:
        flags.append(
            f"identity.self_claim_model '{self_claim}' inconsistent with family/model "
            f"'{model_id}' - treat self-report as unreliable"
        )
    gaps = meta.get("harvest_gaps") or []
    if gaps:
        flags.append(f"meta.harvest_gaps: {list(gaps)}")
    # de-dup preserving order
    seen = set()
    return [f for f in flags if not (f in seen or seen.add(f))]


# ---------------------------------------------------------------- index build

def build_index(standards_dir=None):
    standards = load_standards(standards_dir)
    ids = sorted(standards)
    fps = {mid: extract_fingerprint(standards[mid]) for mid in ids}

    std_summary = {}
    for mid in ids:
        std = standards[mid]
        std_summary[mid] = {
            "file": std.get("_file"),
            "family": (std.get("meta") or {}).get("family"),
            "coverage": {lane: bool(fps[mid][lane]["covered"]) for lane in LANES},
            "corruption_flags": detect_corruption(std),
        }

    pairs = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            cmp_res = compare_pair(standards[a], standards[b])
            cmp_res["a"] = a
            cmp_res["b"] = b
            pairs.append(cmp_res)
    pairs.sort(key=lambda p: p["separability_score"])

    index = {
        "standards_count": len(ids),
        "lane_coverage": {
            lane: sum(1 for mid in ids if fps[mid][lane]["covered"]) for lane in LANES
        },
        "standards": std_summary,
        "pairs": pairs,
        "canonical_validation": validate_canonical(standards, pairs),
    }
    return index


def _resolve_canonical_id(canonical, ids):
    cl = canonical.lower()
    for mid in ids:
        if mid.lower() == cl:
            return mid
    for mid in ids:
        if cl in mid.lower():
            return mid
    return None


def validate_canonical(standards, pairs):
    ids = sorted(standards)
    report = []
    for canonical in CANONICAL_MODELS:
        cid = _resolve_canonical_id(canonical, ids)
        if cid is None:
            report.append({
                "canonical": canonical, "resolved": None,
                "error": "standard not found on disk",
            })
            continue
        inseparable = []
        min_score = None
        for p in pairs:
            if cid not in (p["a"], p["b"]):
                continue
            other = p["b"] if p["a"] == cid else p["a"]
            score = p["separability_score"]
            min_score = score if min_score is None else min(min_score, score)
            if p["separability"] != "separable":
                inseparable.append({
                    "vs": other,
                    "pair_type": p["pair_type"],
                    "score": score,
                    "separability": p["separability"],
                    "lane_status": {l: p["lanes"][l]["status"] for l in LANES},
                })
        report.append({
            "canonical": canonical,
            "resolved": cid,
            "separable_from_all": not inseparable,
            "min_separability_score": min_score,
            "inseparable_pairs": inseparable,
        })
    return report


# ---------------------------------------------------------------- digest

def digest(index, max_pairs=10, max_flagged=8):
    lines = []
    n = index["standards_count"]
    lines.append(f"STANDARDS: {n} loaded")
    cov = index["lane_coverage"]
    lines.append("LANE COVERAGE: " + " ".join(f"{l}={cov[l]}/{n}" for l in LANES))
    flagged = [(mid, s["corruption_flags"]) for mid, s in index["standards"].items() if s["corruption_flags"]]
    lines.append(f"CORRUPTION: {len(flagged)} standards flagged")
    for mid, flags in flagged[:max_flagged]:
        lines.append(f"  - {mid}: {flags[0]}" + (f" (+{len(flags)-1} more)" if len(flags) > 1 else ""))
    lines.append(f"HARDEST PAIRS (lowest separability, top {max_pairs}):")
    for p in index["pairs"][:max_pairs]:
        lines.append(
            f"  {p['a']} | {p['b']} [{p['pair_type']}] score={p['separability_score']} "
            f"-> {p['separability']} strong={p['strong_lanes']}"
        )
    lines.append("CANONICAL VALIDATION:")
    for r in index["canonical_validation"]:
        if r.get("resolved") is None:
            lines.append(f"  {r['canonical']}: STANDARD NOT FOUND")
            continue
        total = len(index["standards"]) - 1
        ok = total - len(r["inseparable_pairs"])
        verdict = "SEPARABLE FROM ALL" if r["separable_from_all"] else f"{len(r['inseparable_pairs'])} INSEPARABLE PAIRS"
        lines.append(
            f"  {r['canonical']} ({r['resolved']}): separable from {ok}/{total} "
            f"min_score={r['min_separability_score']} -> {verdict}"
        )
        for ip in r["inseparable_pairs"][:5]:
            lines.append(f"      ! vs {ip['vs']} [{ip['pair_type']}] lanes={ip['lane_status']}")
    return "\n".join(lines)
