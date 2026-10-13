# backfill_refusal_lane.py -- targeted refusal-lane backfill for thin standards.
#
# Phase 1 of the adaptive-authenticity protocol recovered refusal coverage for
# CANONICAL standards (they already carry top-level refusal.axes). The thin
# distillation-suspect COUNTERPART standards have refusal=None, which makes the
# offline discrimination index mark the refusal lane "unknown" for every pair
# involving them. This script runs ONLY the refusal probe battery (10 short
# temp-0 probes, no judge / T1 / T2 / v2) against a trusted REGISTERED sibling
# lane and merges the signature into each standard's top-level "refusal"
# section -- exactly the shape app/authenticity/diffmatrix.py reads.
#
# ROUTER-ONLY: probes go through the router endpoint key, never upstream.
# QUOTA-BURN GUARD: never probe the standard's meta.model_id (unregistered ->
# broadcast bug). Only the explicit trusted lanes below are probed.
#
# Usage:
#   python scripts/backfill_refusal_lane.py            # dry-run (no writes)
#   python scripts/backfill_refusal_lane.py --apply    # write changes
#   python scripts/backfill_refusal_lane.py --apply --only gpt_codex-gpt-5-6-luna_gpt-6-luna.json
import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import httpx  # noqa: E402
from app.authenticity.probes import run_refusal_probes  # noqa: E402

STANDARDS_DIR = REPO / "app" / "authenticity" / "reference" / "standards"


def _router_self() -> tuple[str, str]:
    key_path = REPO / ".bsl_key"
    key = key_path.read_text(encoding="utf-8").strip() if key_path.exists() else ""
    return "http://127.0.0.1:6969/v1", key


# Explicit standard -> trusted REGISTERED sibling lane (same base model). The
# meta.model_id on these standards is unregistered and is NEVER probed.
# gemini-3.6-flash-high's filename-verified base model is gemini-3.8-flash, so
# it is probed via the registered mintrouter sibling (provider-agnostic lanes).
REFUSAL_LANE_MAP: dict[str, str] = {
    "gpt_codex-gpt-5-6-luna_gpt-6-luna.json":              "codex/gpt-5.6-luna",
    "claude_a6api-claude-fable-5-1_claude-fable-5-1.json": "a6api/claude-fable-5-1",
    "glm_zai-web2-glm-5-3-flash-max_zai-web2.json":        "chat2api/zai-web2/glm-5.3-flash-max",
    "qwen_sharellm-qwen3-7-max_qwen3-7-max.json":          "aibox/qwen3.7-max",
    "qwen_sharellm-qwen3-8-max_qwen3-8-max.json":          "aibox/qwen3.8-max",
    "gemini_antigravity-gemini-3-6-flash-high_gemini-3-8-flash.json": "mintrouter/gemini-3.8-flash",
}


async def backfill(client, endpoint, api_key, path: Path, apply: bool, timeout: int = 60) -> dict:
    std = json.loads(path.read_text(encoding="utf-8"))
    lane = REFUSAL_LANE_MAP.get(path.name)
    if not lane:
        return {"file": path.name, "status": "skip:not_in_refusal_lane_map"}

    existing = (std.get("refusal") or {})
    if existing.get("axes"):
        return {"file": path.name, "status": "skip:already_has_refusal"}

    try:
        sig = await run_refusal_probes(client, endpoint, api_key, lane, timeout)
    except Exception as exc:  # pragma: no cover - network/upstream failure
        return {"file": path.name, "status": f"error:{str(exc)[:100]}", "wrote": False}

    if not isinstance(sig, dict) or not sig.get("axes"):
        return {"file": path.name, "status": "no_signal", "wrote": False}

    refusal = std.setdefault("refusal", {})
    refusal["axes"] = sig.get("axes", {})
    refusal["delta_signature"] = sig.get("delta", {})
    refusal["templates"] = sig.get("templates", {})
    refusal["captured"] = int(sig.get("captured", 0))
    # Record which trusted lane produced the signature (auditability; never the
    # unregistered meta.model_id).
    refusal["probed_via"] = lane

    if apply:
        path.write_text(json.dumps(std, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"file": path.name, "lane": lane, "status": "ok",
            "captured": refusal["captured"], "wrote": bool(apply)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--only", help="only this standard filename")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    endpoint, api_key = _router_self()
    if not api_key:
        print("ERROR: .bsl_key not found")
        return 1
    print(f"endpoint={endpoint}  apply={args.apply}")

    files = sorted(STANDARDS_DIR.glob("*.json"))
    files = [f for f in files if f.name in REFUSAL_LANE_MAP]
    if args.only:
        files = [f for f in files if f.name == args.only]
        if not files:
            print(f"ERROR: no mapped standard named {args.only}")
            return 1
    print(f"refusal backfill targets: {len(files)}\n")

    results = []
    async with httpx.AsyncClient() as client:
        for f in files:
            print(f">> {f.name}  via {REFUSAL_LANE_MAP[f.name]}")
            r = await backfill(client, endpoint, api_key, f, args.apply, args.timeout)
            print(f"   {r['status']}  captured={r.get('captured')}  wrote={r.get('wrote', False)}")
            results.append(r)

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\nDONE: {ok} ok / {len(results) - ok} not-ok of {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
