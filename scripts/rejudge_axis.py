"""Judge-only backfill for a single zeroed noncoding axis (general, 2026-10-09).

Replaces the hardcoded rejudge_creative.py. Re-judges ONE axis of ONE reference
standard by re-asking the anchor lane for that axis's probe outputs (the saved
JSON stores only the score, not the raw text) then re-scoring via the judge.
~2 probe calls + 1 judge call. NEVER re-runs a full lane.

Usage:
  python scripts/rejudge_axis.py --file <std>.json --lane <lane> --axis creative|eq|humor|design
"""
import argparse, asyncio, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.authenticity import harvest as H  # noqa: E402

STD = Path(__file__).resolve().parent.parent / "app" / "authenticity" / "reference" / "standards"
JUDGE = "mintrouter/claude-opus-5-5"
ROUTER = "http://127.0.0.1:6969"


async def rejudge(path: Path, lane: str, axis: str, key: str):
    s = json.loads(path.read_text(encoding="utf-8"))
    node = (s.get("noncoding", {}).get(axis) or {})
    if (node.get("score") or 0) > 0:
        print(f"SKIP {path.name}: {axis} already scored {node.get('score')}")
        return 0
    lane = lane or s["meta"].get("anchor_lane")
    probes = [p for p in H.NONCODING_PROBES if p.get("axis") == axis]
    if not probes:
        print(f"ERR no NONCODING_PROBES for axis={axis}")
        return 1
    outs = []
    async with H._make_client() as client:
        sem = asyncio.Semaphore(2)

        async def one(p):
            async with sem:
                return await H.run_probe(client, ROUTER, key, lane, p, 90, retries=1)

        res = await asyncio.gather(*[one(p) for p in probes])
        for p, r in zip(probes, res):
            if r and not getattr(r, "error", None) and getattr(r, "status_code", 0) == 200:
                outs.append({"axis": axis, "id": p["id"], "content": r.content})
    if not outs:
        print(f"ERR no {axis} outputs re-harvested from {lane}")
        return 1
    scores = await H.judge_scores(ROUTER, key, JUDGE, outs)
    sc = scores.get(axis) or {}
    score = int(sc.get("score", 0) or 0)
    if score <= 0:
        print(f"ERR judge still zeroed {axis}; why={sc.get('why')}")
        return 1
    s["noncoding"][axis] = {"score": score, "why": str(sc.get("why", ""))[:400]}
    fd, tmp = tempfile.mkstemp(dir=str(STD), prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(f"PATCHED {path.name}: {axis} -> {score}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--lane", default="")
    ap.add_argument("--axis", required=True, choices=["creative", "eq", "humor", "design"])
    a = ap.parse_args()
    key = (Path(__file__).resolve().parent.parent / ".bsl_key").read_text(encoding="utf-8").strip()
    return asyncio.run(rejudge(STD / a.file, a.lane, a.axis, key))


if __name__ == "__main__":
    sys.exit(main())
