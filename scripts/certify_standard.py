"""Certify a harvested canonical standard -- Phase 2 of the Harvest Protocol.

The gate that was missing (docs/ARCHITECTURE.md, "Canonical Standard Harvest
Protocol"). A standard is NOT trusted to accuse anyone until all four checks
pass. Zero API calls -- reads only app/authenticity/reference/standards/*.json.

Usage:
    .venv\Scripts\python.exe scripts\certify_standard.py --model claude-opus-5-5
    .venv\Scripts\python.exe scripts\certify_standard.py --file <path-to-json>

Exit code: 0 = certified (all gates pass; SKIP allowed) | 1 = a gate FAILED |
           2 = WARN/ALIAS only (honest alias group recorded, no decisive claim).

Full JSON report persists to .brain/scratch/certify_<model>.json; stdout is a
bounded digest per batch-output discipline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.authenticity import standard_v2 as sv2          # noqa: E402
from app.authenticity import diffmatrix as dm            # noqa: E402
from app.authenticity.harvest import standard_quality    # noqa: E402
from app.authenticity.traitmark import TraitMark         # noqa: E402

_LANES = ("tokenizer", "capability_curve", "knowledge_bank", "discriminator")


def _canonical(std: dict) -> bool:
    return not ((std.get("meta") or {}).get("source_tag") or "").strip()


def _lane_mean(dims: dict):
    vals = [dims.get(l) for l in _LANES if dims.get(l) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _resolve(args):
    if args.file:
        p = Path(args.file)
        std = json.loads(p.read_text(encoding="utf-8"))
        std["_file"] = p.name
        return p.stem, std
    pool = dm.load_standards()
    want = (args.model or "").lower()
    for key, std in pool.items():
        meta = std.get("meta") or {}
        names = {key.lower(), (meta.get("model_id") or "").lower(),
                 (meta.get("sub_model") or "").lower(),
                 (std.get("_file") or "").lower()}
        if want in names:
            return key, std
    return None, None


def certify(key: str, std: dict, pool: dict) -> dict:
    meta = std.get("meta") or {}
    family = (meta.get("family") or "").lower()
    report = {"model": key, "file": std.get("_file"), "family": family,
              "tier": meta.get("tier"), "anchor_lane": meta.get("anchor_lane"),
              "gates": {}, "verdict": "PASS"}

    # Gate 1 -- quality grade full + no unavailable v2 lanes
    q = standard_quality(TraitMark.from_dict(std))
    gaps = list(meta.get("harvest_gaps") or [])
    v2_gaps = [g for g in gaps if "unavailable" in g and g.startswith("v2_")]
    g1 = "PASS" if (q["grade"] == "full" and not v2_gaps) else "FAIL"
    report["gates"]["1_quality"] = {
        "status": g1, "grade": q["grade"], "populated_layers": q["populated_layers"],
        "v2_unavailable_gaps": v2_gaps, "judge_gap": q["judge_gap"]}

    # Gate 2 -- jitter sanity
    v2 = sv2.v2_of(std)
    jitter = ((v2.get("tokenizer") or {}).get("jitter"))
    if jitter is None:
        g2 = "SKIP"
    else:
        g2 = "PASS" if int(jitter) < sv2.UNSTABLE_JITTER else "FAIL"
    report["gates"]["2_jitter"] = {
        "status": g2, "jitter": jitter, "cap": sv2.UNSTABLE_JITTER}

    # Gate 3 -- separability vs every same-family canonical sibling
    sibs = {k: s for k, s in pool.items()
            if k != key and _canonical(s)
            and ((s.get("meta") or {}).get("family") or "").lower() == family}
    alias, unknown, pairs = [], [], {}
    for sk, sib in sorted(sibs.items()):
        r = sv2.pairwise_separability(std, sib)
        pairs[sk] = r.get("distance")
        if r.get("separable") is None:
            unknown.append(sk)
        elif not r["separable"]:
            alias.append(sk)
    if not sibs:
        g3 = "SKIP"
    elif alias:
        g3 = "ALIAS"
    else:
        g3 = "PASS"
    report["gates"]["3_separability"] = {
        "status": g3, "threshold": sv2.SEPARABLE_DISTANCE,
        "sibling_distances": pairs, "alias_group": alias,
        "not_comparable": unknown}

    # Gate 4 -- self-match: standard ranked as suspect vs sibling pool
    cands = {key: std, **sibs}
    dims = sv2.compute_v2_dims(sv2.v2_of(std), cands)
    ranked = sorted(((k, _lane_mean(d)) for k, d in dims.items()),
                    key=lambda kv: (kv[1] is not None, kv[1]), reverse=True)
    ranked = [(k, m) for k, m in ranked if m is not None]
    if not ranked:
        g4, note = "SKIP", "no comparable v2 lanes"
    elif ranked[0][0] != key:
        g4, note = "FAIL", ("self-match MISMATCH: harness bug, NOT provider "
                            "fraud (2026-10-15 retraction lesson); top=%s" % ranked[0][0])
    elif len(ranked) > 1 and ranked[0][1] - ranked[1][1] < sv2.TOKENIZER_DECISIVE_GAP:
        g4, note = "WARN", ("self top but margin %.3f < %.2f vs %s -> abstain zone"
                            % (ranked[0][1] - ranked[1][1],
                               sv2.TOKENIZER_DECISIVE_GAP, ranked[1][0]))
    else:
        g4, note = "PASS", ""
    report["gates"]["4_self_match"] = {
        "status": g4, "note": note,
        "ranked": [{"model": k, "lane_mean": m} for k, m in ranked]}

    statuses = [g["status"] for g in report["gates"].values()]
    if "FAIL" in statuses:
        report["verdict"] = "FAIL"
    elif "ALIAS" in statuses or "WARN" in statuses:
        report["verdict"] = "ALIAS"
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase-2 certification of a harvested standard")
    ap.add_argument("--model", help="model_id / sub_model / filename stem")
    ap.add_argument("--file", help="explicit path to a standard JSON")
    args = ap.parse_args()
    if not args.model and not args.file:
        ap.error("--model or --file required")

    key, std = _resolve(args)
    if std is None:
        print("FAIL: standard '%s' not found in %s" % (args.model, dm.STANDARDS_DIR))
        return 1

    rep = certify(key, std, dm.load_standards())
    out = Path(__file__).resolve().parents[1] / ".brain" / "scratch" / \
        ("certify_%s.json" % key.replace("/", "_"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    print("PERSISTED: %s" % out)
    print("VERDICT: %s  (%s, family=%s, tier=%s, anchor=%s)" % (
        rep["verdict"], rep["file"], rep["family"], rep["tier"], rep["anchor_lane"]))
    for name, g in rep["gates"].items():
        extra = (" -- " + g["note"]) if g.get("note") else ""
        if name == "3_separability" and g.get("alias_group"):
            extra = " -- ALIAS group: " + ", ".join(g["alias_group"])
        print("  %s: %s%s" % (name, g["status"], extra))
    return {"PASS": 0, "ALIAS": 2, "FAIL": 1}[rep["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())