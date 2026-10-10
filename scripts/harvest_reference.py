"""Reference (old-version) standard harvester -- full depth, source_tag='reference'.

These fingerprints are the fake-detection pool: an old model secretly served as
a new one collides against the old version's reference. They are NOT canonical
standards: every one is saved with source_tag='reference', which puts it in its
own file (<family>_<sub>-reference.json) and keeps it OUT of the canonical match
pool (list_testable_standards only reads source_tag=="" canonicals).

Usage:
  python scripts/harvest_reference.py --name claude-sonnet-4-6 --lane mintrouter/claude-sonnet-4-6 --judge codex/gpt-6-astra
"""
import argparse, asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.authenticity import harvest as H  # noqa: E402

ROUTER_EP = "http://127.0.0.1:6969"
REFERENCE_TAG = "reference"


def _depth(tm):
    nc = tm.noncoding
    axes = [(getattr(nc, a, {}) or {}).get("score", 0) for a in H.JUDGE_AXES]
    nc_ok = all(isinstance(s, (int, float)) and s > 0 for s in axes)
    lex_ok = tm.lexical.vocabulary_complexity > 0
    ts = (tm.structural.tokenizer_signature or {})
    v2p = ts.get("v2") or {}
    v2_lanes = [ln for ln in ("tokenizer", "capability_curve", "knowledge_bank") if v2p.get(ln)]
    v2_ok = len(v2_lanes) == 3
    return {"nc": "/".join(str(s) for s in axes), "v2": "+".join(v2_lanes) or "-",
            "full": bool(nc_ok and lex_ok and v2_ok)}


async def _run(name, lane, judge, key, tag):
    throttled = lane.split("/")[0].strip().lower() == "mintrouter"
    cfg = {"probe_timeout_s": 90, "total_timeout_s": 600 if throttled else 300,
           "max_tier": 2, "backoff_gaps": (4.0, 10.0, 25.0),
           "concurrency": 2 if throttled else 8,
           "v2": True, "v2_probe_timeout_s": 180, "v2_total_timeout_s": 2400,
           "v2_concurrency": 2 if throttled else 4}
    tm = await H.harvest_model(
        ROUTER_EP, key, lane,
        judge_endpoint=ROUTER_EP, judge_key=key, judge_model=judge,
        include_noncoding=True, source_tag=tag, config=cfg)
    tm.meta.model_id = name
    # Reference filenames must key off the canonical sub_model, not the lane:
    # a reference re-harvested on a different lane should dedupe to one file.
    tm.meta.sub_model = name
    d = _depth(tm)
    tag = "OK " if d["full"] else "THIN"
    path = H.save_standard(tm, H.STANDARDS_DIR)
    print("%s %-20s nc=%-9s v2=%s judge=%s saved %s" % (tag, name, d["nc"], d["v2"], judge, path.name))
    print("%s" % ("1/1 full-depth" if d["full"] else "0/1 full-depth (saved as reference anyway)"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lane", required=True)
    ap.add_argument("--judge", required=True)
    ap.add_argument("--tag", default=REFERENCE_TAG)
    a = ap.parse_args()
    key = ""
    kf = Path(__file__).resolve().parent.parent / ".bsl_key"
    if kf.exists():
        key = kf.read_text(encoding="utf-8").strip()
    return asyncio.run(_run(a.name, a.lane, a.judge, key, a.tag))


if __name__ == "__main__":
    sys.exit(main())
