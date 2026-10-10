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
}


def _v2_block(d: dict) -> dict:
    """Locate (and if absent create) the nested v2 dict, mirroring v2_of()."""
    ts = d.setdefault("structural", {}).setdefault("tokenizer_signature", {})
    return ts.setdefault("v2", {})


async def _run(file, lane, lane_name, key):
    path = STANDARDS_DIR / file
    d = json.loads(path.read_text(encoding="utf-8"))
    v2 = _v2_block(d)
    if v2.get(lane_name):
        print(f"SKIP {file}: {lane_name} already present")
        return 0
    fn = LANES[lane_name]
    client = _make_client()
    kwargs = dict(timeout_s=180, concurrency=2)
    if lane_name in ("capability_curve", "knowledge_bank"):
        kwargs["k"] = 3 if lane_name == "knowledge_bank" else 2
    res = await fn(client, ROUTER_EP, key, lane, **kwargs)
    if not res:
        print(f"FAIL {file}: {lane_name} returned empty (lane timeout/down)")
        return 1
    v2[lane_name] = res
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK {file}: merged {lane_name} ({len(json.dumps(res))} bytes)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--lane", required=True)
    ap.add_argument("--lane-name", required=True, choices=list(LANES))
    a = ap.parse_args()
    key = (Path(__file__).resolve().parent.parent / ".bsl_key").read_text(encoding="utf-8").strip()
    return asyncio.run(_run(a.file, a.lane, a.lane_name, key))


if __name__ == "__main__":
    sys.exit(main())
