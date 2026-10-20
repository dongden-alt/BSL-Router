"""Targeted capability_curve-only backfill (2026-10-09).

The WRONG approach (which burned calls and clobbered kimi's creative score) was
to re-run the FULL harvest just because capability_curve was missing. This script
runs ONLY the missing v2 lane and MERGES it into the existing standard file --
tokenizer, knowledge_bank, noncoding/judge axes are all preserved untouched.

Usage:
  python scripts/backfill_v2_lane.py --file kimi_kimi-k2-7-code_reference.json \
      --lane aibox/kimi-k2.7-code --lane-name capability_curve
"""
import argparse, asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.authenticity import standard_v2 as sv  # noqa: E402
from app.authenticity.harvest import STANDARDS_DIR, _make_client  # noqa: E402

ROUTER_EP = "http://127.0.0.1:6969"

LANES = {
    "tokenizer": sv.run_tokenizer_fingerprint,
    "capability_curve": sv.run_capability_curve,
    "knowledge_bank": sv.run_knowledge_bank,
    "discriminator": sv.run_discriminator,
    "spatial_curve": sv.run_spatial_curve,
}


def _v2_block(d: dict) -> dict:
    """Locate (and if absent create) the nested v2 dict, mirroring v2_of()."""
    ts = d.setdefault("structural", {}).setdefault("tokenizer_signature", {})
    return ts.setdefault("v2", {})


async def _run(file, lane, lane_name, key, replace=False):
    path = STANDARDS_DIR / file
    d = json.loads(path.read_text(encoding="utf-8"))
    v2 = _v2_block(d)
    existing = v2.get(lane_name)
    if existing and not replace:
        print(f"SKIP {file}: {lane_name} already present")
        return 0
    fn = LANES[lane_name]
    client = _make_client()
    kwargs = dict(timeout_s=180, concurrency=2)
    if lane_name in ("capability_curve", "knowledge_bank", "spatial_curve"):
        kwargs["k"] = 3 if lane_name == "knowledge_bank" else 2
    res = await fn(client, ROUTER_EP, key, lane, **kwargs)
    if not res:
        print(f"FAIL {file}: {lane_name} returned empty (lane timeout/down); file untouched")
        return 1
    if existing and replace:
        old_keys = set((existing.get("answers") or {}))
        new_keys = set((res.get("answers") or {}))
        if old_keys and new_keys and not (old_keys & new_keys):
            # Stale battery (e.g. old sch_* vs new bb_*): rotate wholesale.
            v2[lane_name] = res
            print(f"REPLACE {file}: {lane_name} keyspace rotated "
                  f"{sorted(old_keys)}->{sorted(new_keys)}")
        elif old_keys and new_keys:
            # Same keyspace: merge per-key, new values win.
            out = dict(res)
            merged = dict(existing.get("answers") or {})
            merged.update(res.get("answers") or {})
            out["answers"] = merged
            if existing.get("legacy_features") and res.get("legacy_features"):
                leg = dict(existing["legacy_features"])
                leg.update(res["legacy_features"])
                out["legacy_features"] = leg
            v2[lane_name] = out
            print(f"MERGE {file}: {lane_name} {len(merged)} keys (new values win)")
        else:
            v2[lane_name] = res
            print(f"REPLACE {file}: {lane_name} (no answers keyspace to compare)")
    else:
        v2[lane_name] = res
        print(f"OK {file}: merged {lane_name} ({len(json.dumps(res))} bytes)")
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--lane", required=True)
    ap.add_argument("--lane-name", required=True, choices=list(LANES))
    ap.add_argument("--replace", action="store_true",
                    help="re-run the lane even if present: disjoint keyspaces "
                         "rotate wholesale, overlapping keyspaces merge per-key")
    a = ap.parse_args()
    key = (Path(__file__).resolve().parent.parent / ".bsl_key").read_text(encoding="utf-8").strip()
    return asyncio.run(_run(a.file, a.lane, a.lane_name, key, replace=a.replace))


if __name__ == "__main__":
    sys.exit(main())
